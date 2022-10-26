import numpy as np
import mdtraj as md
from pymbar import MBAR
import jax
import jax.numpy as jnp
from jax import grad
from tqdm import tqdm, trange


class BaseState:
    def __init__(self, temperature, name):
        self._temperature = temperature
        self.name = name

    def calc_energy_frame(self, frame):
        # implement this on your own
        return 0.0

    def calc_energy(self, trajectory):
        # return beta * u
        beta = 1. / self._temperature / 8.314 * 1000.
        eners = []
        for frame in tqdm(trajectory):
            e = self.calc_energy_frame(frame)
            eners.append(e * beta)
        return np.array(eners)


class Sample:
    def __init__(self, trajectory, from_state):
        self.trajectory = trajectory
        self.from_state = from_state
        self.energy_data = {}

    def generate_energy(self, state_list):
        for state in state_list:
            if state.name not in self.energy_data:
                self.energy_data[state.name] = np.array(
                    [state.calc_energy(self.trajectory)])


class MBAREstimator:
    def __init__(self):
        self.samples = []
        self.states = []
        self._mbar = None
        self._umat = None
        self._nk = None
        self._full_samples = None

    def add_sample(self, sample):
        self.samples.append(sample)

    def add_state(self, state):
        self.states.append(state)

    def remove_sample(self, name):
        init_num = len(self.samples)
        self.samples = [s for s in self.samples if s.from_state != name]
        final_num = len(self.samples)
        assert init_num < final_num

    def remove_state(self, name):
        init_num = len(self.states)
        self.states = [s for s in self.states if s.name != name]
        final_num = len(self.states)
        assert init_num < final_num

    def compute_energy_matrix(self):
        for sample in self.samples:
            sample.generate_energy(self.states)

    def _build_umat(self):
        nk_states = {state.name: 0 for state in self.states}
        for sample in self.samples:
            nk_states[sample.from_state] += sample.trajectory.n_frames
        nk_names = [k.name for k in self.states]
        nk = np.array([nk_states[k] for k in nk_states.keys()])
        umat = np.zeros((nk.shape[0], nk.sum()))
        istart = 0
        traj_merge = []
        for nk_name in nk_names:
            for sample in [s for s in self.samples if nk_name == s.from_state]:
                traj_merge.append(sample.trajectory)
                sample_frames = sample.trajectory.n_frames
                iend = istart + sample_frames
                for nnk, nk_name2 in enumerate(nk_names):
                    umat[nnk, istart:iend] = sample.energy_data[nk_name2]
                istart = iend
        return umat, nk, md.join(traj_merge)

    def optimize_mbar(self, initialize="BAR"):
        self.compute_energy_matrix()
        umat, nk, samples = self._build_umat()
        self._umat = umat
        self._nk = nk
        self._full_samples = samples

        self._mbar = MBAR(self._umat, self._nk, initialize=initialize)
        self._umat_jax = jax.numpy.array(self._umat)
        self._free_energy_jax = jax.numpy.array(self._mbar.f_k)
        self._nk_jax = jax.numpy.array(nk)

    def estimate_weight(self, unew, return_freeE=False):
        unew_min = unew.min()
        du_1 = self._free_energy_jax.reshape((-1, 1)) - self._umat_jax
        delta_u = du_1 + unew.reshape((1, -1)) - unew_min - du_1.min()
        cm = 1. / (jax.numpy.exp(delta_u) * jax.numpy.array(self._nk).reshape(
            (-1, 1))).sum(axis=0)
        weight = cm / cm.sum()
        if return_freeE:
            f_new = -jax.numpy.log(jax.numpy.sum(jax.numpy.exp(cm)))
            return weight, f_new
        return weight

    def _estimate_weight_numpy(self, unew_npy, return_cn=False):
        unew_mean = unew_npy.mean()
        du_1 = self._mbar.f_k.reshape((-1, 1)) - self._umat
        delta_u = du_1 + unew_npy.reshape((1, -1)) - unew_mean - du_1.mean()
        cn = 1. / (np.exp(delta_u) * self._nk.reshape((-1, 1))).sum(axis=0)
        weight = cn / cn.sum()
        if return_cn:
            return weight, cn
        else:
            return weight

    def _computeCovar(self, W, N_k):
        K, N = W.shape
        Ndiag = np.diag(N_k)
        I = np.identity(K, dtype=np.float64)

        S2, V = np.linalg.eigh(W @ W.T)
        S2[np.where(S2 < 0.0)] = 0.0
        Sigma = np.diag(np.sqrt(S2))

        # Compute covariance
        Theta = (V @ Sigma @ np.linalg.pinv(
            I - Sigma @ V.T @ Ndiag @ V @ Sigma, rcond=1e-10) @ Sigma @ V.T)
        return Theta

    def compute_covar_mat(self, unew):
        wnew = self._estimate_weight_numpy(unew)
        wappend = np.concatenate(
            [self._mbar.W_nk.T, wnew.reshape((1, -1))], axis=0)

        N_k = np.zeros((self._mbar.N_k.shape[0] + 1, ))
        N_k[:-1] = self._mbar.N_k[:]
        newcov = self._computeCovar(wappend, N_k)
        return newcov

    def compute_variance(self, unew, prop):
        wnew, cn = self._estimate_weight_numpy(unew, return_cn=True)
        ca = (1. / cn).sum()
        A_ave = (prop * wnew).sum()
        W_A = wnew * (prop / A_ave)

        wappend = np.concatenate(
            [self._mbar.W_nk.T,
             wnew.reshape((1, -1)),
             W_A.reshape((1, -1))],
            axis=0)

        N_k = np.zeros((self._mbar.N_k.shape[0] + 2, ))
        N_k[:-2] = self._mbar.N_k[:]

        newcov = self._computeCovar(wappend, N_k)
        return A_ave * A_ave * (newcov[-2, -2] + newcov[-1, -1] -
                                2. * newcov[-1, -2])

    def estimate_effective_sample(self, unew, decompose=False):
        wnew, cn = self._estimate_weight_numpy(unew, return_cn=True)
        eff_samples = 1. / (wnew**2).sum()
        if decompose:
            state_effect = np.zeros((len(self.states)))
            argsort = np.argsort(wnew)[::-1][:int(eff_samples) + 1]
            for nstate in range(state_effect.shape[0]):
                istart = self._nk[:nstate].sum()
                iend = istart + self._nk[nstate]
                state_effect[nstate] = ((argsort > istart) &
                                        (argsort < iend)).sum()
            return eff_samples, state_effect
        return eff_samples

    def estimate_free_energy(self, unew):
        a = self._free_energy_jax - self._umat_jax.T
        # log(sum(n_k*exp(a)))
        a_max = a.max(axis=1, keepdims=True)
        log_denominator_n = jnp.log((self._nk_jax.reshape(
            (1, -1)) * jnp.exp(a - a_max)).sum(axis=1)) + a_max.reshape((-1, ))
        a2 = -unew - log_denominator_n
        # log(sum(exp(a2)))
        a2_max = a2.max()
        f_new = -jnp.log(jnp.sum(jnp.exp(a2 - a2_max))) - a2_max
        return f_new