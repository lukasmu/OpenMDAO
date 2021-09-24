"""Define the FuncComponent class."""

try:
    import jax
    from jax import jvp, vjp, vmap, random, jit
    import jax.numpy as jnp
except ImportError:
    jax = None

import re
import numpy as np
from numpy import asarray, isscalar, imag, complex as npcomplex
from openmdao.core.explicitcomponent import ExplicitComponent
from openmdao.core.constants import INT_DTYPE
from openmdao.utils.units import valid_units
from openmdao.utils.om_warnings import issue_warning
import openmdao.func_api as omf


# regex to check for variable names.
namecheck_rgx = re.compile('[_a-zA-Z][_a-zA-Z0-9]*')

# Names that are not allowed for input or output variables (keywords for options)
_disallowed_varnames = {'has_diag_partials', 'units', 'shape', 'shape_by_conn', 'run_root_only'}

_meta_keep = {'units', 'shape', 'val'}
_from_def = {'default_units': 'units', 'default_shape': 'shape'}


def _check_units_option(option, value):
    if value is not None and not valid_units(value):
        raise ValueError(f"The units '{value}' are invalid.")


def _copy_with_ignore(dct, keepers, warn=True):
    """
    Copy the entries in the given dict whose keys are in keepers.

    Parameters
    ----------
    dct : dict
        The dictionary to be copied.
    keepers : set-like
        Set of keys for entries we want to keep.
    warn : bool
        If True, issue a warning showing which keys were ignored.

    Returns
    -------
    dict
        A new dict containing 'keepers' entries.
    set
        The set of ignored keys.
    """
    kept = {}
    ignored = set()
    for k, v in dct.items():
        if k in keepers:
            kept[k] = v
        else:
            ignored.add(k)

    if warn and ignored:
        issue_warning(f"The following metadata entries were ignored: {sorted(ignored)}.")

    return kept, ignored


class ExplicitFuncComp(ExplicitComponent):
    """
    A component that wraps a python function.

    Parameters
    ----------
    func : function
        The function to be wrapped by this Component.
    **kwargs : named args
        Args passed down to ExplicitComponent.

    Attributes
    ----------
    _func : callable
        The callable wrapped by this component.
    complex_stepsize : float
        Step size used for complex step.
    """

    def __init__(self, func, **kwargs):
        """
        Initialize attributes.
        """
        super().__init__(**kwargs)
        self._func = omf.wrap(func)
        # if complex step is used for derivatives, this is the stepsize
        self.complex_stepsize = 1.e-40

    def initialize(self):
        """
        Declare options.
        """
        self.options.declare('has_diag_partials', types=bool, default=False,
                             desc='If True, treat all array/array partials as diagonal if both '
                                  'arrays have size > 1. All arrays with size > 1 must have the '
                                  'same flattened size or an exception will be raised.')

    def setup(self):
        """
        Define out inputs and outputs.
        """
        for name, meta in self._func.get_input_meta():
            self._check_var_name(name)
            kwargs, _ = _copy_with_ignore(meta, omf._allowed_add_input_args, warn=True)
            self.add_input(name, **kwargs)

        for i, (name, meta) in enumerate(self._func.get_output_meta()):
            if name is None:
                raise RuntimeError(f"{self.msginfo}: Can't add output corresponding to return "
                                   f"value in position {i} because it has no name.  Specify the "
                                   "name by returning a variable, for example 'return myvar', or "
                                   "include the name in the function's return value annotation.")
            self._check_var_name(name)
            kwargs, ignored = _copy_with_ignore(meta, omf._allowed_add_output_args, warn=False)
            ignored.remove('deps')
            if ignored:
                issue_warning(f"The following metadata entries were ignored: {sorted(ignored)}.")
            self.add_output(name, **kwargs)

    def _setup_partials(self):
        """
        Check that all partials are declared.
        """
        decl_partials = super().declare_partials
        hasdiag = self.options['has_diag_partials']
        for out, ometa in self._func.get_output_meta():
            oshp = ometa['shape']
            if not oshp:
                osize = 1
            else:
                osize = np.product(oshp) if isinstance(oshp, tuple) else oshp

            inds = np.arange(osize, dtype=INT_DTYPE)
            for inp, imeta in self._func.get_input_meta():
                if inp not in ometa['deps']:
                    continue

                if hasdiag:
                    ishp = imeta['shape']
                    if not ishp:
                        isize = 1
                    else:
                        isize = np.product(ishp) if isinstance(ishp, tuple) else ishp
                    if osize != isize:
                        raise RuntimeError(f"{self.msginfo}: has_diag_partials is True but "
                                           f"partial({out}, {inp}) is not square "
                                           f"(shape=({osize}, {isize})).")
                    # partial will be declared as diagonal
                    if osize > 1:
                        decl_partials(of=out, wrt=inp, rows=inds, cols=inds)
                    else:
                        decl_partials(of=out, wrt=inp)
                else:
                    decl_partials(of=out, wrt=inp)

        super()._setup_partials()

    def _check_var_name(self, name):
        match = namecheck_rgx.match(name)
        if match is None or match.group() != name:
            raise NameError(f"{self.msginfo}: '{name}' is not a valid variable name.")

        if name in _disallowed_varnames:
            raise NameError(f"{self.msginfo}: cannot use variable name '{name}' because "
                            "it's a reserved keyword.")

    def _compute_output_array(self, input_values, output_array):
        """
        Fill the given output array with our function result based on the given input values.

        Parameters
        ----------
        input_values : tuple of ndarrays or floats
            Unscaled, dimensional input variables.
        output_array
            The output array being filled.
        """
        outs = self._func(*input_values)
        if isinstance(outs, tuple):
            start = end = 0
            for o in outs:
                a = asarray(o) if isscalar(o) else o
                end += a.size
                output_array[start:end] = a.flat
                start = end
        else:
            if isscalar(outs):
                output_array[:] = outs
            else:
                output_array[:] = outs.flat

    def compute(self, inputs, outputs):
        """
        Compute the result of calling our function with the given inputs.

        Parameters
        ----------
        inputs : Vector
            Unscaled, dimensional input variables.
        outputs : Vector
            Unscaled, dimensional output variables.
        """
        # this will update the outputs array in place
        self._compute_output_array(inputs.values(), outputs.asarray())

    def compute_partials(self, inputs, partials):
        """
        Use complex step method to update the given Jacobian.

        Parameters
        ----------
        inputs : `VecWrapper`
            `VecWrapper` containing parameters (p).
        partials : `Jacobian`
            Contains sub-jacobians.
        """
        step = self.complex_stepsize * 1j
        inv_stepsize = 1.0 / self.complex_stepsize
        has_diag_partials = self.options['has_diag_partials']

        in_vals = [np.asarray(v, dtype=npcomplex) for v in inputs.values()]
        result = np.zeros(len(self._outputs), dtype=npcomplex)
        out_slices = self._outputs.get_slice_dict()

        for ivar, inp in enumerate(inputs._abs_iter()):

            if has_diag_partials or in_vals[ivar].size == 1:
                # set a complex input value
                in_vals[ivar] += step

                # solve with complex input value
                self._compute_output_array(in_vals, result)

                for u, slc in out_slices.items():
                    if (u, inp) in self._subjacs_info:
                        partials[(u, inp)] = imag(result[slc] * inv_stepsize)

                # restore old input value
                in_vals[ivar] -= step
            else:
                pval = in_vals[ivar]
                if np.isscalar(pval):
                    # set a complex input value
                    in_vals[ivar] += step

                    # solve with complex input value
                    self._compute_output_array(in_vals, result)

                    for u, slc in out_slices.items():
                        if (u, inp) in self._subjacs_info:
                            # set the column in the Jacobian entry
                            partials[(u, inp)][:, i] = imag(result[slc] * inv_stepsize)

                    # restore old input value
                    in_vals[ivar] -= step
                else:
                    for i in range(pval.size):
                        # set a complex input value
                        in_vals[ivar].flat[i] += step

                        # solve with complex input value
                        self._compute_output_array(in_vals, result)

                        for u, slc in out_slices.items():
                            if (u, inp) in self._subjacs_info:
                                # set the column in the Jacobian entry
                                partials[(u, inp)][:, i] = imag(result[slc] * inv_stepsize)

                        # restore old input value
                        in_vals[ivar].flat[i] -= step
