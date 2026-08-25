set params [dict create]

proc getCaseHome {} {
  global params
  set HOME_BASE "/home/xiaonan/Share/scripts/public/testcase/vivado2"
  set design_name [dict get $params "design_name"]
  set device_name [dict get $params "device_name"]
  set family_name [dict get $params "family_name"]
  return ${HOME_BASE}/${family_name}/${device_name}/${design_name}
}

proc printParams {} {
    global params
    puts "==== params ===="
    dict for {key value} $params {
        puts "$key = $value"
    }
    puts "================"
}

#db_tools_dcp_cmp
proc run_dbtools_compare_dcp {db_tools golden_dcp output_dcp} {
  set out ""
  set st [catch {
    set fp [open "|$db_tools" r+]
    fconfigure $fp -buffering line

    puts $fp "compare_dcp $golden_dcp $output_dcp -txt"
    puts $fp "exit"
    flush $fp

    set out [read $fp]
    close $fp
  } err]

  if {$st != 0} {
    return [list $st $err]
  }
  return [list 0 $out]
}

#db_tools_dcp_shape_cmp
proc run_dbtools_dcpShape_to_txt {db_tools golden_dcp output_dcp} {
  set out ""

  set st [catch {
    set fp [open "|$db_tools" r+]
    fconfigure $fp -buffering line

    puts $fp "dcpShape_to_txt $golden_dcp -cmp"
    puts $fp "dcpShape_to_txt $output_dcp -cmp"
    puts $fp "exit"
    flush $fp

    set out [read $fp]
    close $fp
  } err]

  if {$st != 0} {
    return [list $st $err]
  }

  return [list 0 $out]
}

proc shapeCmp {} {
  global params

  set case_home [getCaseHome]

  puts "======================= start Compare Shape ======================="

  set target_file [file join $case_home golden.dcp]
  set cmp_file [file normalize [file join [pwd] output.dcp]]

  set db_tools "/home/xiaonan/Share/scripts/db_tools/db_tools"

  puts "INFO: target_file -> $target_file"
  puts "INFO: cmp_file -> $cmp_file"

  lassign [run_dbtools_dcpShape_to_txt \
      $db_tools $target_file $cmp_file] st out

  if {$st != 0} {
    error "\033\[1;31mDCP Shape Compare: DbTools failed: $out\033\[0m"
  }

  set target_txt [file join [pwd] golden.dcp.txt]
  set cmp_txt [file join [pwd] output.dcp.txt]

  set target_md5 [lindex [exec md5sum -- $target_txt] 0]
  set cmp_md5 [lindex [exec md5sum -- $cmp_txt] 0]

  if {$target_md5 eq $cmp_md5} {
    puts "\033\[32mDCP Shape Compare PASS : SAME\033\[0m"
    return 0
  } else {
    puts "\033\[31mDCP Shape Compare FAIL : DIFFERENT\033\[0m"
    return 1
  }
}

proc load_config {config_file} {
  global params
  if {[file pathtype $config_file] ne "absolute"} {
    error "config_file must be an absolute path: $config_file"
  }
  if {![file exists $config_file]} {
    error "File not found: $config_file"
  }
  set fh [open $config_file r]
  while {[gets $fh line] >= 0} {
    set line [string trim $line]
    if {$line eq ""} { continue }
    if {[string index $line 0] eq "#"} { continue }
    set hashPos [string first "#" $line]
    if {$hashPos >= 0} {
      set line [string trim [string range $line 0 [expr {$hashPos - 1}]]]
      if {$line eq ""} { continue }
    }
    if {[regexp {^([[:alnum:]_]+)\s+(.+)$} $line -> key value]} {
      set value [string trim $value]
      set v [string tolower $value]
      if {$v in {"1" "true" "on" "yes"}} {
        dict set params $key 1
      } elseif {$v in {"0" "false" "off" "no"}} {
        dict set params $key 0
      } else {
        dict set params $key 0
      }
    } else {
      puts "Warning: invalid line format: $line"
    }
  }
  close $fh
}

proc readEdif {} {
  set edf_file [file join [getCaseHome] golden.edf]

  if {![file exists $edf_file]} {
      error "readEdif: EDF file not found: $edf_file"
  }

  puts "INFO: readEdif -> $edf_file"
  read_edif $edf_file
}

proc readXdc {} {
  set xdc_file [file join [getCaseHome] golden.xdc]

  if {![file exists $xdc_file]} {
      error "readXdc: XDC file not found: $xdc_file"
  }

  puts "INFO: readXdc -> $xdc_file"
  read_xdc $xdc_file
}

proc analyzeDcpInfo {} {
  global params
  set input_dcp "golden_routed.dcp"
  if {[dict get $params "opt_design"] == 1}  {
    set input_dcp "golden.dcp"
  } elseif {[dict get $params "place_design"] == 1}  {
    set input_dcp "golden_opt.dcp"
  } elseif {[dict get $params "place_design_from_syn"] == 1}  {
    set input_dcp "golden.dcp"
  } elseif {[dict get $params "report_utilization"] == 1}  {
    set input_dcp "golden_placed.dcp"
  } elseif {[dict get $params "phys_opt_design"] == 1}  {
    set input_dcp "golden_placed.dcp"
  } elseif {[dict get $params "route_design"] == 1}  {
    set input_dcp "golden_physopt.dcp"
  } elseif {[dict get $params "route_design_from_place"] == 1}  {
    set input_dcp "golden_placed.dcp"
  } elseif {[dict get $params "report_timing_summary"] == 1}  {
    set input_dcp "golden_routed.dcp"
  } elseif {[dict get $params "write_bitstream"] == 1}  {
    set input_dcp "golden_routed.dcp"
  }
  dict set params "input_dcp" $input_dcp

  if {[dict get $params "dcp_cmp"] == 1}  {
    set cmp_golden_dcp ""
    if {[dict get $params "write_bitstream"] == 1}  {
      set cmp_golden_dcp ""
    } elseif {[dict get $params "report_utilization"] == 1}  {
      set cmp_golden_dcp ""
    } elseif {[dict get $params "report_timing_summary"] == 1}  {
      set cmp_golden_dcp ""
    } elseif {[dict get $params "route_design_from_place"] == 1}  {
      set cmp_golden_dcp "golden_routed_from_place.dcp"
    } elseif {[dict get $params "route_design"] == 1}  {
      set cmp_golden_dcp "golden_routed.dcp"
    } elseif {[dict get $params "phys_opt_design"] == 1}  {
      set cmp_golden_dcp "golden_physopt.dcp"
    } elseif {[dict get $params "place_design_from_syn"] == 1}  {
      set cmp_golden_dcp "golden_placed_from_syn.dcp"
    } elseif {[dict get $params "place_design"] == 1}  {
      set cmp_golden_dcp "golden_placed.dcp"
    } elseif {[dict get $params "opt_design"] == 1}  {
      set cmp_golden_dcp "golden_opt.dcp"
    } elseif {[dict get $params "read_edif"] == 1}  {
      set cmp_golden_dcp "golden.dcp"
    }
    dict set params "cmp_golden_dcp" $cmp_golden_dcp
  }
}

proc loadDesign {} {
  global params
  set_device [dict get $params "device_name"]
  set input_dcp [dict get $params "input_dcp"] 
  set case_home [getCaseHome]
  if {[dict get $params "read_edif"] == 1} {
    readEdif
    if {[dict get $params "read_xdc"] == 1} {
    readXdc
  }
  } else {
    read_checkpoint ${case_home}/${input_dcp}
  }
   
}

proc writeCheckpoint {} {
  global params

  write_checkpoint output.dcp

  if {
    [dict get $params read_edif] == 1 &&
    [dict get $params read_xdc] == 1
  } {
    shapeCmp
  }
}

proc reportUtilization {} {
  global params
  report_utilization -file output_utilization.rpt
  set case_home [getCaseHome]
  report_utilization_cmp ${case_home}/golden_utilization_placed.rpt output_utilization.rpt mis_report_utilization.txt
}

proc reportTimingSummary {} {
  global params
  report_timing_summary -file output_timing.rpt -rpx output_timing.rpx
  set case_home [getCaseHome]
  timing_summary_cmp ${case_home}/golden_timing.rpt output_timing.rpt mis_timing_summary.txt
}

proc placeDesign {} {
  global params
  place_design
}

proc physOptDesign {} {
  global params
  phys_opt_design
}

proc routeDesign {} {
  global params
  route_design
}

proc writeBit {} {
  global params
  if {[dict get $params "readback_cmp"] == 1} {
    write_bitstream output.bit -w -m -readback
  } else {
    write_bitstream output.bit -w -m 
  }
}

proc reportBgnOnly {} {
  global params
  write_bitstream output.bit -report_only
  bgnCmp
}

proc bgnCmp {} {
  global params
  set case_home [getCaseHome]
  transform_bgn ${case_home}/golden.bgn golden_cmp.bgn
  transform_bgn output.bgn output_cmp.bgn
  set status [catch {exec /usr/bin/diff golden_cmp.bgn output_cmp.bgn > result_bgn.log} result]
}

proc bitCmp {} {
  global params
  set case_home [getCaseHome]
  bit_cmp ${case_home}/golden.bit output.bit mis_bit.txt
}

proc mskCmp {} {
  global params
  set case_home [getCaseHome]
  bit_cmp ${case_home}/golden.msk output.msk mis_msk.txt
}

proc checksumCmp {} {
  global params
  set case_home [getCaseHome]
  if {[dict get $params "place_design"] == 1 && [dict get $params "checksum_cmp"] == 1 }  {
     checksum_cmp ${case_home}/golden_placed.log run
  }
  if {[dict get $params "route_design"] == 1 && [dict get $params "checksum_cmp"] == 1 }  {
     checksum_cmp ${case_home}/golden_routed.log run
  }
  if {[dict get $params "route_design_from_place"] == 1 && [dict get $params "checksum_cmp"] == 1 }  {
     checksum_cmp ${case_home}/golden_routed_from_place.log run
  }
}

proc rpxCmp {} {
  global params
  set case_home [getCaseHome]
  rpx_cmp ${case_home}/golden_timing.rpx output_timing.rpx mis_rpx.txt
}

proc dcpCmp {} {
  global params
  set case_home [getCaseHome]
  set cmp_golden_dcp [dict get $params "cmp_golden_dcp"]
  set target_file ${case_home}/${cmp_golden_dcp}
  set cmp_file [file normalize [file join [pwd] output.dcp]]

  set db_tools "/home/xiaonan/Share/scripts/db_tools/db_tools"

  if {![file exists $target_file]} {
    error "dcp_cmp: golden file not found: $target_file"
  }
  if {![file isfile $target_file]} {
    error "dcp_cmp: golden path is not a file: $target_file"
  }
  if {![file exists $cmp_file]} {
    error "dcp_cmp: output file not found: $cmp_file"
  }
  if {![file isfile $cmp_file]} {
    error "dcp_cmp: output path is not a file: $cmp_file"
  }

  lassign [run_dbtools_compare_dcp $db_tools $target_file $cmp_file] st out

  if {$st != 0} {
    error "\033\[1;31mdcp_cmp: DbTools failed!\033\[0m"
  }
  if {[regexp {Two Dcps have the same content} $out]} {
    puts "\033\[32mDCP Compare PASS (route) : SAME\033\[0m"
  } elseif {[regexp {Two DCPs are different} $out]} {
    puts "\033\[31mDCP Compare FAIL (route) : DIFFERENT\033\[0m"
  } else {
    puts "No result was captured!"
  }
}

proc readBackCmp {} {
  global params
  set case_home [getCaseHome]
  ascii_bit_cmp ${case_home}/golden.msd output.msd mis_msd.txt 
  ascii_bit_cmp ${case_home}/golden.rbd output.rbd mis_rbd.txt
}

proc argParser {num args} {
  global params
  dict set params "read_edif" 0
  dict set params "read_xdc" 0
  dict set params "opt_design" 0
  dict set params "place_design" 0
  dict set params "place_design_from_syn" 0
  dict set params "report_utilization" 0
  dict set params "phys_opt_design" 0
  dict set params "route_design" 0
  dict set params "route_design_from_place" 0
  dict set params "report_timing_summary" 0
  dict set params "write_bitstream" 0
  dict set params "report_bgn_only" 0
  dict set params "bgn_cmp" 0
  dict set params "bit_cmp" 0
  dict set params "msk_cmp" 0
  dict set params "readback_cmp" 0
  dict set params "write_checkpoint" 0
  dict set params "dcp_cmp" 0
  dict set params "checksum_cmp" 0
  dict set params "rpx_cmp" 0

  dict set params "input_dcp" ""
  dict set params "cmp_golden_dcp" ""

  if {$num == 0} {
    dict set params "write_bitstream" 1
    dict set params "bgn_cmp" 1
    dict set params "bit_cmp" 1
    dict set params "msk_cmp" 1
  } else {
    set arg [lindex $args 0]
    puts $arg
    foreach var $arg {
      if { [file tail $var] eq "flow_config" } { load_config $var }

      if { $var == "read_edif" } {dict set params "read_edif" 1 }
      if { $var == "read_xdc" } {dict set params "read_xdc" 1 }
      if { $var == "opt_design" } {dict set params "opt_design" 1 }
      if { $var == "place_design" } {dict set params "place_design" 1 }
      if { $var == "place_design_from_syn" } {dict set params "place_design_from_syn" 1 }
      if { $var == "report_utilization" } {dict set params "report_utilization" 1 }
      if { $var == "phys_opt_design" } {dict set params "phys_opt_design" 1 }
      if { $var == "route_design" } {dict set params "route_design" 1 }
      if { $var == "route_design_from_place" } {dict set params "route_design_from_place" 1 }
      if { $var == "report_timing_summary" } { dict set params "report_timing_summary" 1 }

      if { $var == "write_bitstream"} {dict set params "write_bitstream" 1}
      if { $var == "report_bgn_only"} {dict set params "report_bgn_only" 1}
      if { $var == "bgn_cmp"} {dict set params "bgn_cmp" 1}
      if { $var == "bit_cmp"} {dict set params "bit_cmp" 1}
      if { $var == "msk_cmp"} {dict set params "msk_cmp" 1}
      if { $var == "checksum_cmp"} {dict set params "checksum_cmp" 1}
      if { $var == "readback_cmp"} {dict set params "readback_cmp" 1}

      if { $var == "write_checkpoint" } { dict set params "write_checkpoint" 1 }
      if { $var == "dcp_cmp"} {dict set params "dcp_cmp" 1}
      if { $var == "report_utilization"} {dict set params "report_utilization" 1}
      if { $var == "rpx_cmp"} {dict set params "rpx_cmp" 1}
    }
  }

  analyzeDcpInfo
  printParams
}

proc runFlow {} {
  global params
  loadDesign
  if {[dict get $params "opt_design"] == 1}  {
     # TODO
  }

  if {[dict get $params "place_design"] == 1 || [dict get $params "place_design_from_syn"] == 1 }  {
    placeDesign 
  }

  if {[dict get $params "report_utilization"] == 1}  {
    reportUtilization
  }

  if {[dict get $params "phys_opt_design"] == 1}  {
    physOptDesign
  }

  if {[dict get $params "route_design"] == 1 || [dict get $params "route_design_from_place"] == 1 }  {
    routeDesign
  }

  if {[dict get $params "report_timing_summary"] == 1}  {
    reportTimingSummary
  }

  # write_bitstream flow
  if {[dict get $params "report_bgn_only"] == 1}  {
    reportBgnOnly
  } elseif {[dict get $params "write_bitstream"] == 1}  {
    writeBit
    if {[dict get $params "bit_cmp"] == 1}  {
      bitCmp
    }
    if {[dict get $params "msk_cmp"] == 1}  {
      mskCmp
    }
    if {[dict get $params "bgn_cmp"] == 1}  {
      bgnCmp
    }
    if {[dict get $params "readback_cmp"] == 1}  {
      readBackCmp 
    }
  }

  if {[dict get $params "checksum_cmp"] == 1}  {
      checksumCmp
  }

  if {[dict get $params "write_checkpoint"] == 1 || [dict get $params "cmp_golden_dcp"] != ""}  {
    writeCheckpoint
  }

  if {[dict get $params "dcp_cmp"] == 1 && [dict get $params "cmp_golden_dcp"] != ""}  {
    dcpCmp
  }
  if {[dict get $params "rpx_cmp"] == 1}  {
      rpxCmp
  }

}
