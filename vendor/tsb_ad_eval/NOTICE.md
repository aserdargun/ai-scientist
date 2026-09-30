# TSB-AD 1.5 vendored metric

`basic_metrics.py` is copied without modifications from the `TSB_AD/evaluation/basic_metrics.py` member of the PyPI `TSB_AD` 1.5 wheel.

- Wheel SHA-256: `4db218b330e8daf845447a714e5367d934d41b67bba4d2310dc41225d962127f`
- Source SHA-256: `1fcddedf5ada1d5221f39ee568c7fddb9e7181bd7e1c19636c2cbecf40c97707`
- Upstream: <https://github.com/TheDatumOrg/TSB-AD>
- License: Apache-2.0; see [LICENSE](LICENSE).

This package is not an application dependency. The application imports only this vendored metric module, and never calls TSB-AD's oracle-threshold wrapper with `pred=None`.
