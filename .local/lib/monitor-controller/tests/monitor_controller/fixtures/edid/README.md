# EDID fixtures

All fixtures are bounded to at most 512 bytes.

- `samsung-broken-captured.hex` is the exact 400-byte fingerprint captured in
  `specs/fixtures/monitor-watcher/samsung-invalid-extension-20260818.fingerprint`.
  Its 128-byte base is valid, it advertises three extension blocks, its second
  complete extension checksum is invalid, and its final advertised block is
  incomplete.
- `samsung-settled-synthetic.hex` preserves that captured Samsung base and the
  available extension bytes, pads the advertised 512-byte length, and repairs
  each extension checksum. It is deliberately synthetic: the repository has no
  complete captured settled DRM EDID. It exercises the settled/readiness shape
  without claiming additional hardware evidence.
- The remaining small fixtures are bounded derivatives for one validation
  failure each.

The `sysfs` fixture trees contain binary copies materialized from these values;
tests copy a tree to a temporary directory before sampling it.
