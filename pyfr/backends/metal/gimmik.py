from gimmik import MetalMatMul
import numpy as np

from pyfr.backends.base import NotSuitableError
from pyfr.backends.metal.provider import MetalKernel, MetalKernelProvider


class MetalGiMMiKKernels(MetalKernelProvider):
    def __init__(self, backend):
        super().__init__(backend)

        # Maximum number of non-zeros
        self.max_nnz = backend.cfg.getint('backend-metal', 'gimmik-max-nnz',
                                          2048)

        # Maximum number of kernels to consider
        self.nkerns = backend.cfg.getint('backend-metal', 'gimmik-nkerns', 18)

        # Number of benchmarking runs
        self.nbench = backend.cfg.getint('backend-metal', 'gimmik-nbench', 40)

        # Kernel cache
        self._mul_kerns = {}

    def mul(self, a, b, out, alpha=1.0, beta=0.0):
        # Ensure the matrices are compatible
        if a.nrow != out.nrow or a.ncol != b.nrow or b.ncol != out.ncol:
            raise ValueError('Incompatible matrices for out = a*b')

        # Check that A is constant
        if 'const' not in a.tags:
            raise NotSuitableError('GiMMiK requires a constant a matrix')

        # Fetch the matrix
        arr = a.get()

        # Check that A is reasonably sparse
        if np.count_nonzero(arr) > self.max_nnz:
            raise NotSuitableError('Matrix too dense for GiMMiK')

        # Dimensions
        ldb, ldc = b.leaddim, out.leaddim

        # Alignment
        if 'align' in b.tags and 'align' in out.tags:
            aligne = self.backend.alignb // b.itemsize
        else:
            aligne = None

        # Cache key
        ckey = (a.mid, alpha, beta, aligne, ldb, ldc)

        # Check the kernel cache
        try:
            kern, grid, tgrp, dt = self._mul_kerns[ckey]
        except KeyError:
            kname = f'gimmik_mm_{arr.shape[0]}x{arr.shape[1]}'
            best_kern = None
            sdata = None

            # Save a copy of the contents of the output matrix
            out_np = getattr(out, 'parent', out).get()

            mm = MetalMatMul(alpha*arr, beta=beta, aligne=aligne, n=b.ncol,
                             ldb=ldb, ldc=ldc)
            kgen = mm.kernels(a.dtype, kname=kname)

            # Benchmark the sequence of kernels generated by GiMMiK
            try:
                for i in range(self.nkerns):
                    src, meta = kgen.send(sdata)

                    grid, tgrp = meta['grid'], meta['threadgroup']
                    kern = self._build_kernel(kname, src, [np.intp]*2)

                    # Obtain the runtime
                    dt = self._benchmark(
                        lambda cbuf: kern(cbuf, grid, tgrp, b.data, out.data),
                        nbench=self.nbench
                    )

                    if best_kern is None or dt < best_kern[-1]:
                        best_kern = kern, grid, tgrp, dt

                    sdata = {'runtime': dt}
            except StopIteration:
                pass

            # Restore the output matrix
            getattr(out, 'parent', out).set(out_np)

            # Update the cache
            self._mul_kerns[ckey] = kern, grid, tgrp, dt = best_kern

        class MulKernel(MetalKernel):
            def run(self, cbuf):
                return kern(cbuf, grid, tgrp, b.data, out.data)

        return MulKernel(mats=[b, out], dt=dt)
