# flow_config keys

Supported keys:
- read_edif
- read_xdc
- report_timing_summary
- opt_design
- place_design
- place_design_from_syn
- phys_opt_design
- route_design
- route_design_from_place
- write_checkpoint
- shape_cmp
- write_bitstream
- bit_cmp
- msk_cmp
- bgn_cmp
- dcp_cmp
- checksum_cmp
- report_utilization
- rpx_cmp
- enable_copy

## shape_cmp

`shape_cmp` is an explicit, non-daily DCP shape-result judge. It requires all
three input/output modules below:

```text
read_edif 1
read_xdc 1
write_checkpoint 1
shape_cmp 1
```

Keep `shape_cmp 0` for ordinary flows. When enabled, the runner requires a
final `Runtime: <seconds>` line and then uses the last `DCP Shape Compare
PASS/FAIL` marker in `run` as the authoritative result.
