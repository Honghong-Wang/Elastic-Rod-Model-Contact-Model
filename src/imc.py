import sys
import mmap
import zmq
import posix_ipc
import numpy as np
import dill as pickle
from numba import njit
from imc_utils import *


class IMC:
    def __init__(self, params):
        # Load parameters
        radius = params['radius']
        self.scale = params['S']
        self.radius = radius * self.scale  # all values normalized by self.scale
        num_nodes = params['num_nodes']
        self.num_edges = num_nodes - 1
        self.collision_limit = params['collision_limit']
        self.contact_stiffness = params['contact_stiffness']
        ce_k, mu_k = params['ce_k'], params['mu_k']
        self.mu_k = mu_k

        self.contact_len = self.radius * 2
        cekr = '_cek_' + str(ce_k) + '_h2_' + str(self.contact_len)
        func_names = ['dd', 'first_grad', 'constant_hess', 'first_hess', 'second_grad' + cekr,
                      'second_hess' + cekr, 'friction_jacobian']

        # Load pre-generated functions
        dir = './grads_hessian_functions/'
        functions = []
        for name in func_names:
            with open(dir + name, 'rb') as f:
                functions.append(pickle.load(f))

        self.dd_grads          = functions[0]
        self.f_grad_funcs      = functions[1]
        self.f_hess_const      = functions[2].reshape((5, 1, 12, 12))
        self.f_hess_func       = functions[3]  # for t2
        self.s_grad_funcs      = functions[4]
        self.s_hess_funcs      = functions[5]
        self.ffr_jacobian_func = functions[6]

        self.friction = 0  # is updated by C++ side

        self.ia = 2  # number of adjacent edges to ignore contact

        # Calculate the number of possible edge combinations ignoring adjacent 5 edges
        # use iterative method to prevent overflow
        num_edge_combos = 0
        for i in range(self.num_edges):
            for j in range(i, self.num_edges):
                if i in range(j-self.ia, j+self.ia+1): continue
                num_edge_combos += 1

        self.indices = np.arange(0, self.num_edges)
        self.edges = np.zeros((self.num_edges, 6), dtype=np.float64)
        self.edge_combos = np.zeros((num_edge_combos, 12), dtype=np.float64)
        self.edge_ids = np.zeros((num_edge_combos, 2), dtype=np.int32)

        ri = 0  # real index
        for i in range(self.num_edges):
            add = self.num_edges - i - (self.ia+1)
            self.edge_ids[ri:ri+add, 0] = i
            self.edge_ids[ri:ri+add, 1] = self.indices[i+self.ia+1:]
            ri += add

        # Sizes for data structures
        nv = num_nodes * 3
        h_size = (nv, nv)
        meta_data_size = 6

        # Initialize shared memory
        self.port_no = sys.argv[1]
        self.forces = np.zeros(nv, dtype=np.float64)
        self.hessian = np.zeros(h_size, dtype=np.float64)
        np.ascontiguousarray(self.forces, dtype=np.float64)
        np.ascontiguousarray(self.hessian, dtype=np.float64)
        assert self.forces.flags['C_CONTIGUOUS'] is True
        assert self.hessian.flags['C_CONTIGUOUS'] is True
        node_bytes = self.forces.nbytes
        hess_bytes = self.hessian.nbytes
        meta_bytes = np.zeros(meta_data_size, dtype=np.float64).nbytes
        n = posix_ipc.SharedMemory('node_coordinates' + self.port_no, size=node_bytes, read_only=False)
        u = posix_ipc.SharedMemory('velocities' + self.port_no, size=node_bytes, read_only=False)
        f = posix_ipc.SharedMemory('contact_forces' + self.port_no, size=node_bytes, read_only=False)
        h = posix_ipc.SharedMemory('contact_hessian' + self.port_no, size=hess_bytes, read_only=False)
        m = posix_ipc.SharedMemory('meta_data' + self.port_no, size=meta_bytes, read_only=False)
        self.node_coordinates = np.ndarray(nv, np.float64, mmap.mmap(n.fd, 0))
        self.velocities = np.ndarray(nv, np.float64, mmap.mmap(u.fd, 0))
        self.forces = np.ndarray(nv, np.float64, mmap.mmap(f.fd, 0))
        self.hessian = np.ndarray(h_size, np.float64, mmap.mmap(h.fd, 0))
        self.meta_data = np.ndarray(meta_data_size, np.float64, mmap.mmap(m.fd, 0))
        assert self.node_coordinates.flags['C_CONTIGUOUS'] is True
        assert self.velocities.flags['C_CONTIGUOUS'] is True
        assert self.forces.flags['C_CONTIGUOUS'] is True
        assert self.hessian.flags['C_CONTIGUOUS'] is True
        assert self.meta_data.flags['C_CONTIGUOUS'] is True

    @staticmethod
    @njit
    def _get_ffr(edges, velocities, forces, mu_k):
        x1s, x1e, x2s, x2e = edges[:, :3], edges[:, 3:6], edges[:, 6:9], edges[:, 9:]
        v1s, v1e, v2s, v2e = velocities[:, :3], velocities[:, 3:6], velocities[:, 6:9], velocities[:, 9:]
        f1s, f1e  = forces[:, :3], forces[:, 3:6]

        num_inputs = edges.shape[0]

        T1 = (x2e - x2s) / np.sqrt(((x2e - x2s)**2).sum(axis=1)).reshape((num_inputs, 1))
        T2 = (x1e - x1s) / np.sqrt(((x1e - x1s)**2).sum(axis=1)).reshape((num_inputs, 1))

        fn = np.sqrt(((f1s + f1e)**2).sum(axis=1))
        ffr_val = mu_k * fn

        v1 = 0.5 * (v1s + v1e)
        v2 = 0.5 * (v2s + v2e)
        vr1 = v1 - v2

        dir1 = np.zeros(num_inputs, dtype=np.float64)
        dir2 = np.zeros(num_inputs, dtype=np.float64)

        # Use for loop here for numba jit compiler. 3D matrix operations unallowed
        for i in range(num_inputs):
            vrx = vr1[i]
            vry, Tx, Ty = -vrx, T1[i], T2[i]
            dir1[i] = vrx.dot(Tx)
            dir2[i] = vry.dot(Ty)

        dir1[dir1 >  0.0] =  1.0
        dir1[dir1 <  0.0] = -1.0
        dir1[dir1 == 0.0] =  0.0
        dir2[dir2 >  0.0] =  1.0
        dir2[dir2 <  0.0] = -1.0
        dir2[dir2 == 0.0] =  0.0

        ffr1 = dir1.reshape((num_inputs, 1)) * T1 * ffr_val.reshape((num_inputs, 1))
        ffr2 = dir2.reshape((num_inputs, 1)) * T2 * ffr_val.reshape((num_inputs, 1))

        ffr = np.zeros((num_inputs, 12), dtype=np.float64)
        ffr1x = 0.50 * (ffr1 - ffr2)
        ffr2x = -ffr1x

        ffr[:, :3]  = 0.50 * ffr1x
        ffr[:, 3:6] = 0.50 * ffr1x
        ffr[:, 6:9] = 0.50 * ffr2x
        ffr[:, 9:]  = 0.50 * ffr2x

        return ffr

    @staticmethod
    @njit
    def _jit_detect_collisions(edges, edge_combos, edge_ids, collision_limit, node_data, contact_len, ia):
        """
            Vectorized collision detection algorithm
        """
        num_edges = edges.shape[0]

        # Construct list of all edge coordinates
        for i in range(num_edges):
            edges[i] = node_data[3*i:(3*i)+6]

        # Construct list of all possible edge combinations (excluding adjacent edges)
        ri = 0  # real index
        for i in range(num_edges):
            base_edge = edges[i]
            add = num_edges - i - (ia + 1)
            edge_combos[ri:ri+add, :6] = base_edge
            edge_combos[ri:ri+add, 6:] = edges[i+ia+1:]
            ri += add

        # Compute the min-distances of all possible edge combinations
        minDs = min_dist_vectorized(edge_combos)

        # Compute the indices of all edge combinations within the collision limit
        col_indices = np.where(minDs - contact_len < collision_limit)

        # Extract data for "in contact" edges
        f_edge_ids = edge_ids[col_indices]

        closest_distance = np.min(minDs)

        return f_edge_ids, closest_distance

    def _detect_collisions(self):
        """ Wrapper function. Numba jit does not work on class methods. """
        return self._jit_detect_collisions(self.edges, self.edge_combos, self.edge_ids, self.collision_limit,
                                           self.node_coordinates, self.contact_len, self.ia)

    @staticmethod
    @njit
    def _jit_prepare_velocities(edge_ids, velocity_data):
        num_edges = edge_ids.shape[0]
        velocities = np.zeros((num_edges, 12), dtype=np.float64)

        for i, ids in enumerate(edge_ids):
            x, y = ids
            velocities[i, :6] = velocity_data[3*x:(3*x)+6]
            velocities[i, 6:] = velocity_data[3*y:(3*y)+6]

        return velocities

    def _prepare_velocities(self, edge_ids):
        return self._jit_prepare_velocities(edge_ids, self.velocities)

    @staticmethod
    @njit
    def _jit_prepare_edges(edge_ids, node_data):
        num_edges = edge_ids.shape[0]
        edge_combos = np.zeros((num_edges, 12), dtype=np.float64)

        for i, ids in enumerate(edge_ids):
            x, y = ids
            edge_combos[i, :6] = node_data[3*x:(3*x)+6]
            edge_combos[i, 6:] = node_data[3*y:(3*y)+6]
        dists, f_out_vals = min_dist_f_out_vectorized(edge_combos)

        closest_distance = np.min(dists)

        return edge_combos, closest_distance, f_out_vals

    def _prepare_edges(self, edge_ids):
        return self._jit_prepare_edges(edge_ids, self.node_coordinates)

    @staticmethod
    @njit
    def _optimize_chain_rule(dd_grads, f_grad_vals, s_grad_vals, s_hess_vals_pre, s_hess_vals, dE_dx):
        num_inputs = s_grad_vals.shape[0]
        # Perform chain rule for contact gradient (forces)
        # dE/dx = dE/dd1 * dd1/dx + dE/dd2 * dd2/dx + and so on
        dE_dx[:] += s_grad_vals[:, :9] @ dd_grads
        s_grad_vals = s_grad_vals.reshape((num_inputs, 15, 1))
        for i in range(6):
            dE_dx[:] += s_grad_vals[:, i+9] * f_grad_vals[i]

        # Perform chain rule to obtain d^2E/dd1x, d^2E/dd2x, and so on
        # These are necessary to compute the chain rule for hessian.
        s_hess_vals_pre = s_hess_vals_pre.reshape((15, num_inputs, 15, 1))
        for i in range(15):
            curr_s = s_hess_vals_pre[i]
            for j in range(9):
                s_hess_vals[i, :] += curr_s[:, j] * dd_grads[j]
            for j in range(6):
                s_hess_vals[i, :] += curr_s[:, j+9] * f_grad_vals[j]

    def _chain_rule_contact_hess(self, f_grad_vals, s_grad_vals, f_hess_vals, s_hess_vals_pre):
        num_inputs = s_grad_vals.shape[0]
        dE_dx = np.zeros((num_inputs, 12), dtype=np.float64)
        d2E_dx2 = np.zeros((num_inputs, 12, 12), dtype=np.float64)

        # Optimize chain rule for code that involves only 2D arrays using numba jit
        s_hess_vals = np.zeros((15, num_inputs, 12), dtype=np.float64)

        self._optimize_chain_rule(self.dd_grads, f_grad_vals, s_grad_vals, s_hess_vals_pre, s_hess_vals, dE_dx)

        # Perform chain rule for contact hessian
        # d^2E/dx = d^2E/dd1x * dd1/dx + dE/dd1 * d^2d1/dx^2 + and so on
        s_grad_vals = s_grad_vals.reshape((num_inputs, 15, 1, 1))
        s_hess_vals = s_hess_vals.reshape((15, num_inputs, 12, 1))
        f_grad_vals = f_grad_vals.reshape((6, num_inputs, 1, 12))
        dd = self.dd_grads.reshape((9, 1, 12))
        for i in range(9):
            d2E_dx2[:] += s_hess_vals[i] @ dd[i]
        for i in range(5):
            d2E_dx2[:] += s_grad_vals[:, i+9] * self.f_hess_const[i]
            d2E_dx2[:] += s_hess_vals[i + 9] @ f_grad_vals[i]
        d2E_dx2[:] += s_grad_vals[:, -1] * f_hess_vals
        d2E_dx2[:] += s_hess_vals[-1] @ f_grad_vals[-1]

        return dE_dx, d2E_dx2

    @staticmethod
    @njit
    def _chain_rule_contact_nohess(dd_grads, f_grad_vals, s_grad_vals):
        num_inputs = s_grad_vals.shape[0]
        dE_dx = np.zeros((num_inputs, 12), dtype=np.float64)
        dE_dx[:] += s_grad_vals[:, :9] @ dd_grads
        s_grad_vals = s_grad_vals.reshape((num_inputs, 15, 1))
        for i in range(6):
            dE_dx[:] += s_grad_vals[:, i+9] * f_grad_vals[i]

        return dE_dx

    @staticmethod
    @njit
    def _get_ffr_jacobian_inputs(edges, velocities, dE_dx, mu_k):
        num_inputs = edges.shape[0]
        ffr_jac_input = np.zeros((num_inputs, 37), dtype=np.float64)

        ffr_jac_input[:, :12] += edges
        ffr_jac_input[:, 12:24] += velocities
        ffr_jac_input[:, 24:36] += dE_dx
        ffr_jac_input[:, 36] += mu_k

        return ffr_jac_input

    @staticmethod
    def _chain_rule_friction_jacobian(ffr_grad_s, d2E_dx2):
        """  This function is more efficient without numba njit
             since we can do 3D matrix operation without for loop. """

        num_inputs = ffr_grad_s.shape[0]
        ffr_jacobian = np.zeros((num_inputs, 12, 12), dtype=np.float64)

        ffr1_grad = ffr_grad_s[:, :3, :12] + ffr_grad_s[:, :3, 12:] @ d2E_dx2
        ffr2_grad = ffr_grad_s[:, 3:, :12] + ffr_grad_s[:, 3:, 12:] @ d2E_dx2
        ffr_jacobian[:, :3]  += ffr1_grad
        ffr_jacobian[:, 3:6] += ffr1_grad
        ffr_jacobian[:, 6:9] += ffr2_grad
        ffr_jacobian[:, 9:]  += ffr2_grad

        return ffr_jacobian

    @staticmethod
    @njit
    def _jit_py_to_cpp_hess(py_forces, py_jacobian, cpp_forces, cpp_jacobian, edge_ids):
        for i in range(py_forces.shape[0]):
            forces = py_forces[i]
            jacobian = py_jacobian[i]

            # Enter into global force and hessian container.
            e1, e2 = edge_ids[i]

            cpp_forces[(3 * e1):(3 * e1) + 6] += forces[:6]
            cpp_forces[(3 * e2):(3 * e2) + 6] += forces[6:]

            cpp_jacobian[3*e1:3*e1+6, 3*e1:3*e1+6] += jacobian[:6, :6]
            cpp_jacobian[3*e1:3*e1+6, 3*e2:3*e2+6] += jacobian[:6, 6:]
            cpp_jacobian[3*e2:3*e2+6, 3*e1:3*e1+6] += jacobian[6:, :6]
            cpp_jacobian[3*e2:3*e2+6, 3*e2:3*e2+6] += jacobian[6:, 6:]

    def _py_to_cpp_hess(self, py_forces, py_jacobian, edge_ids):
        self._jit_py_to_cpp_hess(py_forces, py_jacobian, self.forces, self.hessian, edge_ids)

    @staticmethod
    @njit
    def _py_to_cpp_nohess(py_forces, cpp_forces, edge_ids):
        for i in range(py_forces.shape[0]):
            forces = py_forces[i]

            # Enter into global force and hessian container.
            e1, e2 = edge_ids[i]

            cpp_forces[(3 * e1):(3 * e1) + 6] += forces[:6]
            cpp_forces[(3 * e2):(3 * e2) + 6] += forces[6:]

    def _get_forces(self, contact_points):
        edges, edge_ids, velocities, s_input_vals = contact_points

        num_inputs = edges.shape[0]

        # Obtain first contact energy gradients
        f_grad_vals = np.array([f_grad(*edges) for f_grad in self.f_grad_funcs], dtype=np.float64).squeeze()

        # Get second contact energy gradients
        s_grad_vals = np.array([s_grad(*s_input_vals) for s_grad in self.s_grad_funcs], dtype=np.float64).T

        # Reshape data structures for proper indexing in case of only one collision
        if num_inputs == 1:
            s_grad_vals = s_grad_vals.reshape((1, 15))
            f_grad_vals = f_grad_vals.reshape((6, 1, 12))

        # Perform chain ruling to get contact gradient and hessian
        dE_dx = self._chain_rule_contact_nohess(self.dd_grads, f_grad_vals, s_grad_vals)

        # Calculate friction forces on all four nodes
        ffr = self._get_ffr(edges, velocities, dE_dx, self.mu_k)

        if self.friction:
            total_forces = dE_dx + ffr
        else:
            total_forces = dE_dx

        # Check to make sure no nan or infs are added to simulator
        f_test = np.sum(total_forces)
        assert not np.isnan(f_test), 'Force had a nan'
        assert not np.isinf(f_test), 'Force had an inf'

        # Write gradient and hessian to shared memory location for DER
        self._py_to_cpp_nohess(total_forces, self.forces, edge_ids)

        # Apply contact stiffness to gradient and hessian
        self.forces[:] *= self.contact_stiffness

    def _get_forces_and_hessian(self, contact_points):
        edges, edge_ids, velocities, s_input_vals = contact_points

        num_inputs = edges.shape[0]

        # Obtain first contact energy gradients
        f_grad_vals = np.array([f_grad(*edges) for f_grad in self.f_grad_funcs], dtype=np.float64).squeeze()

        # Obtain first contact energy hessians
        f_hess_vals = np.array(self.f_hess_func(*edges), dtype=np.float64)

        # Get second contact energy gradients
        s_grad_vals = np.array([s_grad(*s_input_vals) for s_grad in self.s_grad_funcs], dtype=np.float64).T

        # Get second contact energy hessians
        s_hess_vals_pre = np.array([s_hess(*s_input_vals) for s_hess in self.s_hess_funcs], dtype=np.float64).squeeze()

        # Reshape data structures for proper indexing in case of only one collision
        if num_inputs == 1:
            s_grad_vals = s_grad_vals.reshape((1, 15))
            s_hess_vals_pre = s_hess_vals_pre.reshape((15, 1, 15))
            f_grad_vals = f_grad_vals.reshape((6, 1, 12))

        # Perform chain ruling to get contact gradient and hessian
        dE_dx, d2E_dx2 = self._chain_rule_contact_hess(f_grad_vals, s_grad_vals, f_hess_vals, s_hess_vals_pre)

        # Prepare inputs for friction force functions
        ffr_jacobian_input = self._get_ffr_jacobian_inputs(edges, velocities, dE_dx, self.mu_k)

        # Calculate friction forces on all four nodes
        ffr = self._get_ffr(edges, velocities, dE_dx, self.mu_k)

        # Calculate the incomplete friction force gradients
        ffr_grad_s = self.ffr_jacobian_func(*ffr_jacobian_input).reshape((num_inputs, 6, 24))

        ffr_jacobian = self._chain_rule_friction_jacobian(ffr_grad_s, d2E_dx2)

        if self.friction:
            total_forces = dE_dx + ffr
            total_jacobian = d2E_dx2 + ffr_jacobian
        else:
            total_forces = dE_dx
            total_jacobian = d2E_dx2

        # Check to make sure no nan or infs are added to simulator
        try:
            f_test = np.sum(total_forces)
            h_test = np.sum(total_jacobian)
            assert not np.isnan(f_test), 'Force had a nan'
            assert not np.isinf(f_test), 'Force had an inf'
            assert not np.isnan(h_test), 'Jacobian had a nan'
            assert not np.isinf(h_test), 'Jacobian had an inf'
        except AssertionError:
            total_jacobian = d2E_dx2  # remove ffr jacobian

        # Write gradient and hessian to shared memory location for DER
        self._py_to_cpp_hess(total_forces, total_jacobian, edge_ids)

        # Apply contact stiffness to gradient and hessian
        self.forces[:]  *= self.contact_stiffness
        self.hessian[:] *= self.contact_stiffness

    def _update_contact_stiffness(self, curr_cd, last_cd):
        diff = curr_cd - last_cd
        if curr_cd > self.contact_len + 0.005 and diff > 0:
            self.contact_stiffness *= 0.999
        elif diff < 0:
            if curr_cd < self.contact_len - 0.004:
                self.contact_stiffness *= 1.01
            elif curr_cd < self.contact_len - 0.002:
                self.contact_stiffness *= 1.005
            elif curr_cd < self.contact_len - 0.001:
                self.contact_stiffness *= 1.003
            elif curr_cd < self.contact_len:
                self.contact_stiffness *= 1.001

    def start_server(self):
        # Initialize ZMQ socket.
        context = zmq.Context()
        socket = context.socket(zmq.REP)
        socket.bind("tcp://*:{}".format(self.port_no))
        print("Connected to python server")

        edge_ids = None
        velocities = None
        closest_distance = 0
        last_cd = 0

        while 1:
            # block until DER gives msg
            socket.recv()

            hessian = int(self.meta_data[5])
            first_iter = int(self.meta_data[0])
            self.friction = int(self.meta_data[1])

            # Scale the nodal coordinates by scaling factor
            self.node_coordinates *= self.scale

            # Run collision detection algorithm and get edge ids at the start of every time step
            if first_iter:
                self.velocities *= self.scale
                edge_ids, closest_distance = self._detect_collisions()

            # Reset all gradient and hessian values to 0
            self.forces[:] = 0.
            if hessian: self.hessian[:] = 0.

            # Generate forces is contact is detected
            num_con = edge_ids.shape[0]
            if num_con != 0:

                # Obtain edge combinations and velocities
                if first_iter: velocities = self._prepare_velocities(edge_ids)
                edge_combos, closest_distance, f_out_vals = self._prepare_edges(edge_ids)

                # Increase/decrease contact stiffness depending on penetration severity
                if first_iter: self._update_contact_stiffness(closest_distance, last_cd)

                # If contact exists, compute contact gradient and hessian
                contact_data = (edge_combos, edge_ids, velocities, f_out_vals)

                if not hessian:
                    self._get_forces(contact_data)
                else:
                    self._get_forces_and_hessian(contact_data)

            self.meta_data[4] = closest_distance / self.scale

            # Unblock DER
            socket.send(b'')

            # After each time step, print out summary information
            if first_iter:
                last_cd = closest_distance
                print("time: {:.4f} | iters: {} | con: {:03d} | min_dist: {:.6f} | "
                      "k: {:.3e} | fric: {}".format(self.meta_data[2],
                                                    int(self.meta_data[3]),
                                                    num_con,
                                                    self.meta_data[4],
                                                    self.contact_stiffness,
                                                    self.friction))


def main():
    np.seterr(divide='ignore', invalid='ignore')

    # Simulation
    col_limit  = float(sys.argv[2])
    cont_stiff = float(sys.argv[3])
    ce_k       = float(sys.argv[4])
    mu_k       = float(sys.argv[5])
    radius     = float(sys.argv[6])
    num_nodes  = int(sys.argv[7])
    S          = float(sys.argv[8])

    params = {'num_nodes': num_nodes,
              'radius': radius,
              'collision_limit': col_limit,
              'contact_stiffness': cont_stiff,
              'ce_k': ce_k,
              'S': S,
              'mu_k': mu_k}

    contact_model = IMC(params)
    contact_model.start_server()


if __name__ == '__main__':
    main()
