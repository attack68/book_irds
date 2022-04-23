from datetime import datetime, timedelta
import math
from math import ceil
from copy import deepcopy
import numpy as np
from modules.dual import Dual


def exp(x):
    if isinstance(x, Dual):
        return x.__exp__()
    return math.exp(x)


def log(x):
    if isinstance(x, Dual):
        return x.__log__()
    return math.log(x)


def interpolate(x, x_1, y_1, x_2, y_2, interpolation, start=None):
    if interpolation == "linear":
        op = lambda z: z
    elif interpolation == "log_linear":
        op, y_1, y_2 = exp, log(y_1), log(y_2)
    elif interpolation == "linear_zero_rate":
        y_1 = log(y_1) / ((start - x_1) / timedelta(days=365))
        y_2 = log(y_2) / ((start - x_2) / timedelta(days=365))
        op = lambda z: exp((start-x)/timedelta(days=365) * z)
    ret = op(y_1 + (y_2 - y_1) * (x - x_1) / (x_2 - x_1))
    return ret


class Curve:

    def __init__(self, nodes: dict, interpolation: str):
        self.nodes = deepcopy(nodes)
        self.interpolation = interpolation

    def __getitem__(self, date: datetime):
        node_dates = list(self.nodes.keys())
        for i, node_date_1 in enumerate(node_dates[1:]):
            if date <= node_date_1 or i == len(node_dates) - 2:
                node_date_0 = node_dates[i]
                return interpolate(
                    date,
                    node_date_0,
                    self.nodes[node_date_0],
                    node_date_1,
                    self.nodes[node_date_1],
                    self.interpolation,
                    node_dates[0]
                )

    def __repr__(self):
        output = ""
        for k, v in self.nodes.items():
            output += f"{k.strftime('%Y-%b-%d')}: {v:.6f}\n"
        return output


def add_months(start: datetime, months: int) -> datetime:
    """add a given number of months to an input date with a modified month end rule"""
    year_roll = int((start.month + months - 1) / 12)
    month = (start.month + months) % 12
    month = 12 if month == 0 else month
    try:
        end = datetime(start.year + year_roll, month, start.day)
    except ValueError:  # day is out of range for month
        return add_months(datetime(start.year, start.month, start.day-1), months)
    else:
        return end


class Schedule:

    def __init__(self, start: datetime, tenor: int, period: int, days=False):
        self.start = start
        self.end = add_months(start, tenor)
        self.tenor = tenor
        self.period = period
        self.dcf_conv = timedelta(days=365)
        self.n_periods = ceil(tenor / period)

    def __repr__(self):
        output = "period start | period end | period DCF\n"
        for period in self.data:
            output += f"{period[0].strftime('%Y-%b-%d')} | " \
                      f"{period[1].strftime('%Y-%b-%d')} | {period[2]:3f}\n"
        return output

    @property
    def data(self):
        schedule = []
        period_start = self.start
        for i in range(self.n_periods - 1):
            period_end = add_months(period_start, self.period)
            schedule.append(
                [period_start, period_end, (period_end - period_start) / self.dcf_conv]
            )
            period_start = period_end
        schedule.append(
            [period_start, self.end, (self.end - period_start) / self.dcf_conv]
        )
        return schedule


class SolvedCurve(Curve):
    def __init__(self, nodes: dict, interpolation: str, swaps: list, obj_rates: list,
                 algorithm: str = "gauss_newton"):
        super().__init__(nodes=nodes, interpolation=interpolation)
        self.swaps, self.obj_rates, self.algo = swaps, obj_rates, algorithm
        self.n, self.m = len(self.nodes.keys()) - 1, len(self.swaps)
        self.s = np.array([self.obj_rates]).transpose()
        self.lam = 1000

    def calculate_metrics(self):
        self.r = np.array([[swap.rate(self) for swap in self.swaps]]).transpose()
        self.v = np.array([[v for v in list(self.nodes.values())[1:]]]).transpose()
        x = self.r - self.s
        self.f = np.matmul(x.transpose(), x)[0][0]
        self.grad_v_f = np.array(
            [[self.f.dual.get(f"v{i+1}", 0) for i in range(self.n)]]
        ).transpose()
        self.J = np.array([
            [rate.dual.get(f"v{j+1}", 0) for rate in self.r[:, 0]]
            for j in range(self.n)
        ])

    def update_step_gradient_descent(self):
        y = np.matmul(self.J.transpose(), self.grad_v_f)
        alpha = np.matmul(y.transpose(), self.r - self.s) / np.matmul(y.transpose(), y)
        alpha = alpha[0][0].real
        v_1 = self.v - self.grad_v_f * alpha
        return v_1

    def update_step_gauss_newton(self):
        A = np.matmul(self.J, self.J.transpose())
        b = -0.5 * self.grad_v_f
        delta = np.linalg.solve(A, b)
        v_1 = self.v + delta
        return v_1

    def update_step_levenberg_marquardt(self):
        self.lam *= 2 if self.f_prev < self.f.real else 0.5
        J_T = self.J.transpose()
        A = np.matmul(self.J, J_T) + self.lam * np.eye(self.J.shape[0])
        b = -0.5 * self.grad_v_f
        delta = np.linalg.solve(A, b)
        v_1 = self.v + delta
        return v_1

    def iterate(self, max_i=2000, tol=1e-10):
        ret, self.f_prev, self.f_list = None, 1e10, []
        for i in range(max_i):
            self.calculate_metrics()
            self.f_list.append(self.f.real)
            if self.f.real < self.f_prev and (self.f_prev - self.f.real) < tol:
                ret = f"tolerance reached ({self.algo}) after {i} iterations, "
                ret += f"func: {self.f.real}"
                break
            v_1 = getattr(self, f"update_step_{self.algo}")()
            for i, (k, v) in enumerate(self.nodes.items()):
                if i == 0:
                    continue
                self.nodes[k] = v_1[i - 1, 0]
            self.f_prev = self.f.real
        self.lam = 1000
        return f"max iterations ({self.algo}), f: {self.f.real}" if ret is None else ret

    @property
    def grad_s_v(self):
        if getattr(self, "grad_s_v_", None) is None:
            self.grad_s_v_numeric()
        return self.grad_s_v_

    def grad_s_v_numeric(self, **kwargs):
        kwargs = {
            "interpolation": self.interpolation,
            "nodes": self.nodes,
            "algorithm": "gauss_newton",
            "swaps": self.swaps,
            "obj_rates": self.obj_rates,
            **kwargs
        }
        grad_s_v = np.zeros(shape=(self.m, self.n))
        ds = 1e-2
        s_cv_fwd = type(self)(**kwargs)
        for s in range(self.m):
            s_cv_fwd.nodes, s_cv_fwd.s = self.nodes, self.s.copy()
            s_cv_fwd.s[s, 0] += ds
            print("fwd", s_cv_fwd.iterate())
            dvds_fwd = np.array([v.real for v in (s_cv_fwd.v[:, 0] - self.v[:, 0])/ds])
            grad_s_v[s, :] = dvds_fwd
        self.grad_s_v_ = grad_s_v


class Swap:

    def __init__(
        self,
        start: datetime,
        tenor: int,
        period_fix: int,
        period_float: int,
    ):
        self.start = start
        self.end = add_months(start, tenor)
        self.schedule_fix = Schedule(start, tenor, period_fix)
        self.schedule_float = Schedule(start, tenor, period_float)

    def __repr__(self):
        return f"<Swap: {self.start.strftime('%Y-%m-%d')} -> " \
               f"{self.end.strftime('%Y-%m-%d')}>"

    def analytic_delta(self, curve: Curve, leg: str = "fix", notional: float = 1e4):
        delta = 0
        for period in getattr(self, f"schedule_{leg}").data:
            delta += curve[period[1]] * period[2]
        return delta * notional / 10000

    def rate(self, curve: Curve):
        rate = (curve[self.start] - curve[self.end]) / self.analytic_delta(curve)
        return rate * 100

    def npv(self, curve: Curve, fixed_rate: float, notional: float = 1e6):
        npv = (self.rate(curve) - fixed_rate) * self.analytic_delta(curve)
        return npv * notional / 100

    def risk(self, curve: SolvedCurve, fixed_rate: float, notional: float = 1e6):
        grad_v_P = np.array([
            [self.npv(curve, fixed_rate, notional).dual.get(f"v{i+1}", 0)
             for i in range(curve.n)]
        ]).transpose()
        grad_s_P = np.matmul(curve.grad_s_v, grad_v_P)
        return grad_s_P / 100