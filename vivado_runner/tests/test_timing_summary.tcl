# Offline command-dispatch test; no Vivado installation required.
source [file join [file dirname [info script]] .. .. run_proc.tcl]
set calls {}
proc getCaseHome {} { return /golden/case }
proc report_timing_summary {args} { lappend ::calls [linsert $args 0 report] }
proc timing_summary_cmp {args} { lappend ::calls [linsert $args 0 rpt] }
proc rpx_cmp {args} { lappend ::calls [linsert $args 0 rpx] }
reportTimingSummary
set expected [list \
    [list report -file output_timing.rpt -rpx output_timing.rpx] \
    [list rpt /golden/case/golden_timing.rpt output_timing.rpt mis_timing_summary.txt] \
    [list rpx /golden/case/golden_timing.rpx output_timing.rpx mis_rpx.txt]]
if {$calls ne $expected} { error "Unexpected timing command sequence: $calls" }
puts "PASS timing report generates and compares both RPT and RPX"
