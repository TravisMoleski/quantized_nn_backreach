'''
Backreach using quantized inputs
'''

from typing import List, Tuple, Dict, TypedDict, Optional

import time
from copy import deepcopy
from math import floor, ceil

import numpy as np
import matplotlib.pyplot as plt

from star import Star
from plotting import Plotter
from dubins import init_to_constraints, get_time_elapse_mat
from util import make_qstar, is_init_qx_qy
from networks import get_cmd

from timerutil import timed, Timers
from settings import Quanta
from parallel import run_all_parallel, increment_index, shared_num_counterexamples, worker_had_counterexample

class State():
    """state of backreach container

    state is:
    alpha_prev (int) - previous command 
    """

    debug = False

    nn_update_rate = 1.0
    next_state_id = 0

    def __init__(self, alpha_prev: int, qtheta1: int, qv_own: int, qv_int: int, \
                 star: Star):

        assert isinstance(qtheta1, int)
        self.qtheta1 = qtheta1
        self.qv_own = qv_own
        self.qv_int = qv_int      
        
        self.alpha_prev_list = [alpha_prev]
        
        self.star = star
        self.domain_witness = star.is_feasible()
        assert self.domain_witness is not None
        
        self.state_id = -1
        self.assign_state_id()

    def __str__(self):
        return f"State(id={self.state_id} with alpha_prev_list = {self.alpha_prev_list})"

    @timed
    def copy(self, new_star=None, new_domain_witness=None):
        """return a deep copy of self"""

        if new_star is not None:
            assert new_domain_witness is not None
            self_star = self.star
            self.star = None

        rv = deepcopy(self)
        rv.assign_state_id()

        if new_star is not None:
            rv.star = new_star
            rv.domain_witness = new_domain_witness
            
            # restore self.star
            self.star = self_star

        return rv

    def print_replay_init(self):
        """print initialization states for replay"""

        print(f"alpha_prev_list = {self.alpha_prev_list}")
        print(f"qtheta1 = {self.qtheta1}")
        print(f"qv_own = {self.qv_own}")
        print(f"qv_int = {self.qv_int}")

    def print_replay_witness(self, plot=False):
        """print a step-by-step replay for the witness point"""

        s = self
        
        domain_pt, range_pt, rad = s.star.get_witness(get_radius=True)
        print(f"chebeshev point radius: {rad}")
        print(f"end = np.{repr(domain_pt)}\nstart = np.{repr(range_pt)}")

        if rad < 1e-6:
            print("WARNING: radius was tiny, skipping replay (may mismatch due to numerics)")
        else:

            p = Plotter()

            print()

            pt = range_pt.copy()

            q_theta1 = s.qtheta1
            s_copy = deepcopy(s)

            p.plot_star(s.star, color='r')
            mismatch = False

            pos_quantum = Quanta.pos

            for i in range(len(s.alpha_prev_list) - 1):
                net = s.alpha_prev_list[-(i+1)]
                expected_cmd = s.alpha_prev_list[-(i+2)]

                dx = floor((pt[Star.X_INT] - pt[Star.X_OWN]) / pos_quantum)
                dy = floor((0 - pt[Star.Y_OWN]) / pos_quantum)

                qstate = (dx, dy, q_theta1, s.qv_own, s.qv_int)

                cmd_out = get_cmd(net, *qstate)
                print(f"({i+1}). network {net} -> {cmd_out}")
                print(f"state: {list(pt)}")
                print(f"qstate: {qstate}")

                if cmd_out != expected_cmd:
                    print(f"Mismatch at step {i+1}. got cmd {cmd_out}, expected cmd {expected_cmd}")
                    mismatch = True
                    break

                s_copy.backstep(forward=True, forward_alpha_prev=cmd_out)
                p.plot_star(s_copy.star)

                mat = get_time_elapse_mat(cmd_out, 1.0)
                pt = mat @ pt

                delta_q_theta = Quanta.cmd_quantum_list[cmd_out]# * theta1_quantum
                q_theta1 += delta_q_theta

            if plot:
                plt.show()

            if mismatch:
                print("mismatch in replay... was the chebyshev center radius tiny?")
            else:
                print("witness commands all matched expectation")

    def assign_state_id(self):
        """assign and increment state_id"""

        self.state_id = State.next_state_id
        State.next_state_id += 1

    @timed
    def backstep(self, forward=False, forward_alpha_prev=-1):
        """step backwards according to alpha_prev"""

        if forward:
            cmd = forward_alpha_prev
        else:
            cmd = self.alpha_prev_list[-1]

        assert 0 <= cmd <= 4

        mat = get_time_elapse_mat(cmd, -1.0 if not forward else 1.0)

        #if forward:
        #    print(f"condition number of transform mat: {np.linalg.cond(mat)}")
        #    print(f"condition number of a_mat: {np.linalg.cond(self.star.a_mat)}")

        self.star.a_mat = mat @ self.star.a_mat
        self.star.b_vec = mat @ self.star.b_vec

        # clear, weak left, weak right, strong left, strong right
        delta_q_theta = Quanta.cmd_quantum_list[cmd]

        if forward:
            self.qtheta1 += delta_q_theta
        else:
            self.qtheta1 -= delta_q_theta

    @timed
    def get_predecessors(self, plotter=None, stdout=False):
        """get the valid predecessors of this star
        """

        dx_qrange, dy_qrange = self.get_dx_dy_qrange(stdout=stdout)

        # compute previous state
        self.backstep()

        if plotter is not None:
            plotter.plot_star(self.star)

        rv: List[State] = []
        dx_qrange, dy_qrange = self.get_dx_dy_qrange(stdout=stdout)

        # pass 1: do all quantized states classify the same (correct way)?
        # alternatively: are they all incorrect?

        # pass 2: if partial correct / incorrect, are any of the incorrect ones feasible?
        # if so, we may need to split the set

        constants = (self.qtheta1, self.qv_own, self.qv_int)

        for prev_cmd in range(5):
            qstate_to_cmd: Dict[Tuple[int, int], int] = {}
            all_right = True
            all_wrong = True
            correct_qstates = []
            incorrect_qstates = []
            
            for qdx in range(dx_qrange[0], dx_qrange[1] + 1):
                for qdy in range(dy_qrange[0], dy_qrange[1] + 1):
                    qstate = (qdx, qdy)

                    # skip predecessors that are also initial states
                    if is_init_qx_qy(qdx, qdy):
                        continue

                    out_cmd = get_cmd(prev_cmd, qdx, qdy, *constants)

                    qstate_to_cmd[qstate] = out_cmd

                    if out_cmd == self.alpha_prev_list[-1]:
                        # command is correct
                        correct_qstates.append(qstate)
                        all_wrong = False
                    else:
                        # command is wrong
                        incorrect_qstates.append(qstate)
                        all_right = False

            if all_wrong:
                continue
            
            # no splitting; all correct
            if all_right:
                prev_s = self.copy()
                prev_s.alpha_prev_list.append(prev_cmd)

                rv.append(prev_s)
                continue

            # still have a chance at all correct (no splitting): if all incorrect state are infeasible
            all_incorrect_infeasible = True

            for qstate in incorrect_qstates:
                star = make_qstar(self.star, qstate)

                if star.is_feasible() is not None:
                    all_incorrect_infeasible = False
                    break

            # no splitting; all incorrect states were infeasible
            if all_incorrect_infeasible:
                prev_s = self.copy()
                prev_s.alpha_prev_list.append(prev_cmd)

                rv.append(prev_s)
                continue

            # need to split based on if command is correct
            for qstate in correct_qstates:
                star = make_qstar(self.star, qstate)
                domain_witness = star.is_feasible()
                
                if domain_witness is not None:
                    prev_s = self.copy(star, domain_witness)
                    prev_s.alpha_prev_list.append(prev_cmd)

                    rv.append(prev_s)

        return rv

    @timed
    def get_dx_dy_qrange(self, stdout=False) -> Tuple[Tuple[int, int], Tuple[int, int]]:
        """get the quantized range for (dx, dy)"""

        pos_quantum = Quanta.pos
        vec = np.zeros(Star.NUM_VARS)

        # dx = x_int - x_own
        vec[Star.X_INT] = 1
        vec[Star.X_OWN] = -1
        dx_min = self.star.minimize_vec(vec) @ vec
        dx_max = self.star.minimize_vec(-vec) @ vec

        qdx_min = floor(dx_min / pos_quantum)
        qdx_max = ceil(dx_max / pos_quantum)
        dx_qrange = (qdx_min, qdx_max)

        # dy = y_int - y_own
        vec = np.zeros(Star.NUM_VARS)
        #vec[Star.Y_INT] = 1 # Y_int is always 0
        vec[Star.Y_OWN] = -1

        dy_min = self.star.minimize_vec(vec) @ vec
        dy_max = self.star.minimize_vec(-vec) @ vec
        
        qdy_min = floor(dy_min / pos_quantum)
        qdy_max = ceil(dy_max / pos_quantum)
        dy_qrange = (qdy_min, qdy_max)

        return dx_qrange, dy_qrange

class BackreachResult(TypedDict):
    counterexample: Optional[State]
    runtime: float
    num_popped: int
    unique_paths: int
    index: int
    params: Tuple[int, int, int, int, int, int]

def backreach_single(arg, parallel=True, plot=False) -> Optional[BackreachResult]:
    """run backreachability from a single symbolic state"""

    if parallel:
        index, params = increment_index()
    else:
        assert arg is not None
        index = 0
        params = arg

    init_alpha_prev, x_own, y_own, theta1, v_own, v_int = params

    start = time.perf_counter()

    box, a_mat, b_vec = init_to_constraints(x_own, y_own, v_own, v_int, theta1)

    init_star = Star(box, a_mat, b_vec)
    init_s = State(init_alpha_prev, theta1, v_own, v_int, init_star)

    work = [init_s]
    popped = 0

    rv: BackreachResult = {'counterexample': None, 'runtime': np.inf, 'params': params,
                           'num_popped': 0, 'unique_paths': 0, 'index': index}
    deadends = set()

    plotter: Optional[Plotter] = None

    if plot:
        plotter = Plotter()
        plotter.plot_star(init_s.star, 'r')

    while work and rv['counterexample'] is None:
        s = work.pop()
        popped += 1

        predecessors = s.get_predecessors(plotter=plotter)

        for p in predecessors:
            work.append(p)
            
            if p.alpha_prev_list[-2] == 0 and p.alpha_prev_list[-1] == 0:
                # also check if > 20000 ft
                domain_pt = p.domain_witness
                assert domain_pt is not None
                pt = p.star.domain_to_range(domain_pt)

                dx = (pt[Star.X_INT] - pt[Star.X_OWN])
                dy = (0 - pt[Star.Y_OWN])
                
                if dx**2 + dy**2 > 10000**2:
                    rv['counterexample'] = deepcopy(p)

                    if parallel:
                        with shared_num_counterexamples.get_lock():
                            shared_num_counterexamples.value += 1
                            print(f"\nIndex {index}. found Counterexample (count: {shared_num_counterexamples.value}) ",
                                  end='', flush=True)

                    break

        if not predecessors:
            deadends.add(tuple(s.alpha_prev_list))

    diff = time.perf_counter() - start
    rv['runtime'] = diff
    rv['num_popped'] = popped
    rv['unique_paths'] = len(deadends)

    if rv['counterexample'] is not None and parallel:
        worker_had_counterexample(rv)

    return rv

def run_single_case():
    """test a single (difficult) case"""

    print("running single...")

    alpha_prev=4
    x_own=(-3, -2)
    y_own=(-4, -3)
    qtheta1=152
    q_vown=2
    q_vint=7
    #num_popped: 1043422, unique_paths: 5551, has_counterexample: False

    Timers.tic('top')
    params = (alpha_prev, x_own, y_own, qtheta1, q_vown, q_vint)
    res = backreach_single(params, parallel=False, plot=False)
    Timers.toc('top')
    Timers.print_stats()

    if res is not None:
        print(f"popped: {res['num_popped']}")
        print(f"unique_paths: {res['unique_paths']}")

    plt.show()

def main():
    """main entry point"""

    Quanta.init_cmd_quantum_list()

    Counterexamples (235): [408, 407, 379, 380, 378, 418, 414, 381, 415, 419, 859, 860, 858, 872, 893, 887, 892, 888, 894, 861, 895, 898, 899, 1339, 1337, 1338, 1336, 1340, 1352, 1366, 1342, 1367, 1373, 1374, 1372, 1375, 1378, 1379, 1368, 1824, 1823, 1820, 1819, 1818, 1817, 1822, 1816, 1846, 1852, 1854, 1855, 1821, 1832, 1858, 1847, 1859, 2303, 2297, 2304, 2305, 2300, 2296, 2298, 2325, 2332, 2312, 2326, 2335, 2302, 2334, 2299, 2331, 2301, 2343, 2342, 2338, 2327, 2339, 2777, 2783, 2780, 2779, 2776, 2781, 2785, 2784, 2782, 2796, 2805, 2815, 2810, 2807, 2812, 2814, 2811, 2823, 2822, 2806, 2778, 2792, 2819, 2818, 4705, 4732, 4738, 4743, 4733, 4739, 4742, 5184, 5183, 5185, 5211, 5212, 5218, 5219, 5222, 5223, 5665, 5663, 5664, 5662, 5691, 5697, 5696, 5698, 5692, 5702, 5699, 5703, 6144, 6141, 6143, 6142, 6140, 6156, 6171, 6176, 6177, 6178, 6179, 6145, 6172, 6182, 6183, 6620, 6623, 6621, 6628, 6626, 6636, 6627, 6622, 6624, 6650, 6651, 6656, 6659, 6658, 6666, 6667, 6662, 6663, 9548, 9517, 9543, 9537, 10011, 9997, 10023, 10027, 10017, 10491, 10506, 10507, 10503, 10497, 10949, 10947, 10986, 10983, 10987, 11427, 11428, 11455, 11429, 11460, 11461, 11463, 11467, 11466, 14797, 15301, 15761, 15781, 15792, 16241, 16261, 42647, 43127, 43607, 44085, 44091, 44565, 44571, 44570, 45045, 45051, 45050, 46971, 47451, 47931, 48411, 48895, 52735, 53215, 53695, 2324092, 2324093, 2324572, 2324573, 2325052, 2325053, 2325521, 2325536, 2325532, 2325533, 2326017, 2326012, 2326016, 2326013, 2326015, 2328897, 2329377, 2329857]

    #run_single_case()
    run_all_parallel(backreach_single, index=1822)
    #run_all_parallel(backreach_single)

    

    # 373.
if __name__ == "__main__":
    main()
