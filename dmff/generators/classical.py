from ..api.topology import DMFFTopology
from ..api.paramset import ParamSet
from ..utils import DMFFException, isinstance_jnp
from ..admp.pme import setup_ewald_parameters
import numpy as np
import jax.numpy as jnp
import openmm.app as app
import openmm.unit as unit
from ..classical.inter import CoulNoCutoffForce, CoulombPMEForce, CoulReactionFieldForce, LennardJonesForce


class CoulombGenerator:
    def __init__(self, ffinfo: dict, paramset: ParamSet):
        self.name = "CoulombForce"
        self.ffinfo = ffinfo
        self.paramset = paramset
        self.coulomb14scale = float(
            self.ffinfo["Forces"]["CoulombForce"]["meta"]["coulomb14scale"])
        self._use_bcc = False

    def overwrite(self):
        # paramset to ffinfo
        pass

    def createPotential(self, topdata: DMFFTopology, nonbondedMethod,
                        nonbondedCutoff, args):
        methodMap = {
            app.NoCutoff: "NoCutoff",
            app.CutoffPeriodic: "CutoffPeriodic",
            app.CutoffNonPeriodic: "CutoffNonPeriodic",
            app.PME: "PME",
        }
        if nonbondedMethod not in methodMap:
            raise DMFFException("Illegal nonbonded method for NonbondedForce")

        isNoCut = False
        if nonbondedMethod is app.NoCutoff:
            isNoCut = True

        mscales_coul = jnp.array([0.0, 0.0, 0.0, 1.0, 1.0,
                                  1.0])  # mscale for PME
        mscales_coul = mscales_coul.at[2].set(self.coulomb14scale)

        # set PBC
        if nonbondedMethod not in [app.NoCutoff, app.CutoffNonPeriodic]:
            ifPBC = True
        else:
            ifPBC = False

        charges = [a.meta["charge"] for a in topdata.atoms()]
        charges = jnp.array(charges)

        cov_mat = topdata.buildCovMat()

        if unit.is_quantity(nonbondedCutoff):
            r_cut = nonbondedCutoff.value_in_unit(unit.nanometer)
        else:
            r_cut = nonbondedCutoff

        # PME Settings
        if nonbondedMethod is app.PME:
            cell = topdata.getPeriodicBoxVectors()
            box = jnp.array(cell)
            self.ethresh = args.get("ethresh", 1e-6)
            self.coeff_method = args.get("PmeCoeffMethod", "openmm")
            self.fourier_spacing = args.get("PmeSpacing", 0.1)
            kappa, K1, K2, K3 = setup_ewald_parameters(r_cut, self.ethresh,
                                                       box,
                                                       self.fourier_spacing,
                                                       self.coeff_method)

        if nonbondedMethod is not app.PME:
            # do not use PME
            if nonbondedMethod in [app.CutoffPeriodic, app.CutoffNonPeriodic]:
                # use Reaction Field
                coulforce = CoulReactionFieldForce(
                    r_cut,
                    charges,
                    isPBC=ifPBC,
                    topology_matrix=cov_mat if self._use_bcc else None)
            if nonbondedMethod is app.NoCutoff:
                # use NoCutoff
                coulforce = CoulNoCutoffForce(
                    topology_matrix=cov_mat if self._use_bcc else None)
        else:
            coulforce = CoulombPMEForce(
                r_cut,
                kappa, (K1, K2, K3),
                topology_matrix=cov_mat if self._use_bcc else None)

        coulenergy = coulforce.generate_get_energy()

        def potential_fn(positions, box, pairs, params):

            # check whether args passed into potential_fn are jnp.array and differentiable
            # note this check will be optimized away by jit
            # it is jit-compatiable
            isinstance_jnp(positions, box, params)

            if self._use_bcc:
                coulE = coulenergy(positions, box, pairs, charges,
                                   params["CoulombForce"]["bcc"], mscales_coul)
            else:
                coulE = coulenergy(positions, box, pairs, charges,
                                   mscales_coul)

            return coulE

        self._jaxPotential = potential_fn
        return potential_fn


class LennardJonesGenerator:
    def __init__(self, ffinfo: dict, paramset: ParamSet):
        self.name = "LennardJonesForce"
        self.ffinfo = ffinfo
        self.paramset = paramset
        self.lj14scale = float(
            self.ffinfo["Forces"][self.name]["meta"]["lj14scale"])
        self.atype_to_idx = {}
        self.atype_to_idx["_dummy_"] = 0
        sig_prms, eps_prms = [], []
        sig_prms.append(1.0)
        eps_prms.append(0.0)
        for node in self.ffinfo["Forces"][self.name]["node"]:
            if node["name"] != "Atom":
                continue
            if "type" in node["attrib"]:
                atype, eps, sig = node["attrib"]["type"], node["attrib"][
                    "epsilon"], node["attrib"]["sigma"]
                if atype in self.atype_to_idx:
                    raise DMFFException(f"Repeat L-J parameters for {atype}")
                self.atype_to_idx[atype] = len(sig_prms)
            elif "class" in node["attrib"]:
                acls, eps, sig = node["attrib"]["class"], node["attrib"][
                    "epsilon"], node["attrib"]["sigma"]
                atypes = [
                    k for k in ffinfo["Type2Class"].keys()
                    if ffinfo["Type2Class"][k] == acls
                ]
                for atype in atypes:
                    if atype in self.atype_to_idx:
                        raise DMFFException(
                            f"Repeat L-J parameters for {atype}")
                    self.atype_to_idx[atype] = len(sig_prms)
            sig_prms.append(float(sig))
            eps_prms.append(float(eps))
        sig_prms = jnp.array(sig_prms)
        eps_prms = jnp.array(eps_prms)

        sig_nbf, eps_nbf = [], []

        paramset.addField(self.name)
        paramset.addParameter(sig_prms, "sigma", field=self.name)
        paramset.addParameter(eps_prms, "epsilon", field=self.name)
        paramset.addParameter(sig_nbf, "sigma_nbfix", field=self.name)
        paramset.addParameter(eps_nbf, "epsilon_nbfix", field=self.name)

    def overwrite(self):
        # paramset to ffinfo
        for nnode in range(len(self.ffinfo["Forces"][self.name]["node"])):
            if node["name"] != "Atom":
                continue
            if "type" in node["attrib"]:
                atype = node["attrib"]["type"]
                idx = self.atype_to_idx[atype]

            elif "class" in node["attrib"]:
                acls = node["attrib"]["class"]
                atypes = [
                    k for k in self.ffinfo["Type2Class"].keys()
                    if self.ffinfo["Type2Class"][k] == acls
                ]
                idx = self.atype_to_idx[atypes[0]]

            eps_now = self.paramset[self.name]["epsilon"][idx]
            sig_now = self.paramset[self.name]["sigma"][idx]
            self.ffinfo["Forces"][
                self.name]["node"][nnode]["attrib"]["sigma"] = sig_now
            self.ffinfo["Forces"][
                self.name]["node"][nnode]["attrib"]["epsilon"] = eps_now

    def createPotential(self, topdata: DMFFTopology, nonbondedMethod,
                        nonbondedCutoff, args):
        methodMap = {
            app.NoCutoff: "NoCutoff",
            app.CutoffPeriodic: "CutoffPeriodic",
            app.CutoffNonPeriodic: "CutoffNonPeriodic",
            app.PME: "CutoffPeriodic",
        }
        if nonbondedMethod not in methodMap:
            raise DMFFException("Illegal nonbonded method for NonbondedForce")
        methodString = methodMap[nonbondedMethod]

        atoms = [a for a in topdata.atoms()]
        atypes = [
            a.meta["type"] if
            ("type" in a.meta and a.meta is not None) else "_dummy_"
            for a in atoms
        ]
        map_prm = [self.atype_to_idx[atype] for atype in atypes]

        # not use nbfix for now
        map_nbfix = []
        map_nbfix = np.array(map_nbfix, dtype=int).reshape((-1, 3))

        if methodString in ["NoCutoff", "CutoffNonPeriodic"]:
            isPBC = False
            if methodString == "NoCutoff":
                isNoCut = True
            else:
                isNoCut = False
        else:
            isPBC = True
            isNoCut = False

        mscales_lj = jnp.array([0.0, 0.0, 0.0, 1.0, 1.0, 1.0])  # mscale for LJ
        mscales_lj = mscales_lj.at[2].set(self.lj14scale)

        if unit.is_quantity(nonbondedCutoff):
            r_cut = nonbondedCutoff.value_in_unit(unit.nanometer)
        else:
            r_cut = nonbondedCutoff

        ljforce = LennardJonesForce(0.0,
                                    r_cut,
                                    map_prm,
                                    map_nbfix,
                                    isSwitch=False,
                                    isPBC=isPBC,
                                    isNoCut=isNoCut)
        ljenergy = ljforce.generate_get_energy()

        def potential_fn(positions, box, pairs, params):

            # check whether args passed into potential_fn are jnp.array and differentiable
            # note this check will be optimized away by jit
            # it is jit-compatiable
            isinstance_jnp(positions, box, params)

            ljE = ljenergy(positions, box, pairs,
                           params["LennardJonesForce"]["epsilon"],
                           params["LennardJonesForce"]["sigma"],
                           params["LennardJonesForce"]["epsilon_nbfix"],
                           params["LennardJonesForce"]["sigma_nbfix"],
                           mscales_lj)

            return ljE

        self._jaxPotential = potential_fn
        return potential_fn