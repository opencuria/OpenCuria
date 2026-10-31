# Portable coverage review synopsis

Historical runner/QEMU acceptance has 279 mapped R/Q groups; [the exact named test map](catalog-coverage.json) is retained with [passing final JUnit](full-fresh-cache-final.xml). The eight optional runner skips are outside mapped lifecycle acceptance cases. Frontend F001–F082 map to 86 named assertions in the final 2247-test suite. U001 also has headed browser evidence.

U007 remains a cross-layer assertion: frontend tests cover image readiness/version choice and generic resource validation; frontend metadata does not expose the selected image disk minimum. Backend admission and real-VM V016/V048 cover that minimum. No sole frontend assertion is claimed for it.

Real VM matrices retain their exact earlier runtime digests. Later power/cache hardening was verified by the full final runner suite and subsequent fault, crash and full-desktop runs. Backend complete baselines and focused final projection delta have separate source manifests. The original longer review and failed raw attempts remain workspace-local.
