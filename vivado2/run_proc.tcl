set testcase_dir [file dirname $full_path]
set testcase_name [lindex $path_list end-1]
set device_str [lindex $path_list end-2]

set options_args [lrange $argv 0 end]

########### parse option and select&&check .dcp ###################
set dcp_suffix_dict [ dict create \
  ""  0 \
  "_opt" 0 \
  "_placed" 0 \
  "_physopt" 0 \
  "_routed" 0 
]
set maxThreads 0
set option_init ""
set read_edf 0
foreach option $options_args {
  puts "#$option"
  if { $option == "maxThreads" } {
    set maxThreads  1
  } elseif { $option == "read_edif" } {
    set read_edf 1
  } elseif { $option == "opt_design" } {
    dict set dcp_suffix_dict "" 1
  } elseif { $option == "place_design_from_syn" } {
    dict set dcp_suffix_dict "" 1
  } elseif { $option == "place_design" } {
    dict set dcp_suffix_dict "_opt" 1
  } elseif { $option == "report_utilization_from_place" } {
    dict set dcp_suffix_dict "_placed" 1
  } elseif { $option == "phys_opt_design" } {
    dict set dcp_suffix_dict "_placed" 1
  } elseif { $option == "route_design_from_place" } {
    dict set dcp_suffix_dict "_placed" 1
  } elseif { $option == "route_design" } {
    dict set dcp_suffix_dict "_physopt" 1
  } elseif { $option == "report_timing_summary" } {
    dict set dcp_suffix_dict "_routed" 1
  } elseif { $option == "write_sdf" } {
    dict set dcp_suffix_dict "_routed" 1
  } elseif { $option == "write_verilog_timesim" } {
    dict set dcp_suffix_dict "_routed" 1
  } elseif { $option == "write_bitstream" } {
    dict set dcp_suffix_dict "_routed" 1
  } elseif { $option == "init_from_synth" || $option == "init_from_opt" || $option == "init_from_place" || $option == "init_from_route" } {
    set option_init $option
  }
}

set dcp_file ""
set dcp_enc_file ""

if { $read_edf == 0 } {
  set dcp_suffix ""
  if { $option_init == "" } {
    dict for {key value} $dcp_suffix_dict {
      if { $value == 1 } {
        set dcp_suffix $key
        break
      }
    }
  } else {
    if { $option_init == "init_from_synth" } {
      set dcp_suffix "_init"
    } elseif { $option_init == "init_from_opt" } {
      set dcp_suffix "_opt_init"
    } elseif { $option_init == "init_from_place" } {
      set dcp_suffix "_placed_init"
    } elseif { $option_init == "init_from_route" } {
      set dcp_suffix "_routed_init"
    }
  }
  set dcp_file ${testcase_dir}/golden${dcp_suffix}.dcp
  set dcp_enc_file ${testcase_dir}/golden${dcp_suffix}_enc.dcp
}

set output_name "vivado"
if { [info exists is_gen] } {
  set output_name ${device_str}--${testcase_name}
}

if { $read_edf == 0  } {
  if {![file exists $dcp_file]} {
    set dcp_file_old $dcp_file
    set dcp_file $dcp_enc_file
    if {![file exists $dcp_file]} {
      puts "#File dcp not exists. File Path: ${dcp_file_old}"
      exec rm ${output_name}.log -rf
      exit 1
    }
  }
}

########### vivado commands ##############

##### set maxThreads ####
if { $maxThreads == 0 } {
  puts "#set_param general.maxThreads 1"
  set_param general.maxThreads 1
}

if { $read_edf == 0 } {
  read_checkpoint $dcp_file
  if { $option_init == "init_from_synth" } {
    set xdc_file ${testcase_dir}/golden_init.xdc
    if {[file exists $xdc_file]} {
      read_xdc $xdc_file
    }
  }
} else {
  set edf_file ${testcase_dir}/golden.edf
  read_edif $edf_file
  set xdc_file ${testcase_dir}/golden.xdc
  if {[file exists $xdc_file]} {
    read_xdc $xdc_file
  }
}
link_design -part $device_str 

set gen_dcp_suffix ""
if { $option_init != "" } {
  if { $option_init == "init_from_synth" } {
    set gen_dcp_suffix "_synth"
  } elseif { $option_init == "init_from_opt" } {
    set gen_dcp_suffix "_opt"
  } elseif { $option_init == "init_from_place" } {
    place_design -unplace
    set gen_dcp_suffix "_opt"
  } elseif { $option_init == "init_from_route" } {
    route_design -unroute
    place_design -unplace
    set gen_dcp_suffix "_opt"
  }
  return
}

if { $read_edf == 1 } {
  set gen_dcp_suffix "_synth"
}

if { "opt_design" in $options_args } {
  opt_design
  set gen_dcp_suffix "_opt"
}

if { "place_design" in $options_args || "place_design_from_syn" in $options_args } {
  set start_time [clock seconds]
  catch { place_design } error_message
  if { $error_message != "" } {
    puts "#place_failed: $error_message"
  }
  set end_time [clock seconds]
  set elapsed_time [expr {$end_time - $start_time}]
  puts "#Command place_design execution time: $elapsed_time seconds"
  if { "place_design" in $options_args } {
    set gen_dcp_suffix "_placed"
  } else {
    set gen_dcp_suffix "_placed_from_syn"
  }
}

if { "report_utilization_from_place" in $options_args } {
  report_utilization -file ${output_name}_utilization_placed.rpt
  set gen_dcp_suffix ""
}

if { "phys_opt_design" in $options_args } {
  catch { phys_opt_design } error_message
  if { $error_message != "" } {
    puts "#phys_opt_design: $error_message"
  }
  set gen_dcp_suffix "_physopt"
}

if { "route_design" in $options_args || "route_design_from_place" in $options_args } {
  set start_time [clock seconds]
  catch { route_design } error_message
  if { $error_message != "" } {
    puts "#route_failed: $error_message"
  }
  set end_time [clock seconds]
  set elapsed_time [expr {$end_time - $start_time}]
  puts "#Command route_design execution time: $elapsed_time seconds"
  if { "route_design" in $options_args } {
    set gen_dcp_suffix "_routed"
  } else {
    set gen_dcp_suffix "_routed_from_place"
  }
}

if { "report_timing_summary" in $options_args } {
  report_timing_summary -file ${output_name}_timing.rpt -rpx ${output_name}_timing.rpx
  set gen_dcp_suffix ""
}

if { "write_sdf" in $options_args } {
  write_sdf -file ${output_name}_routed.sdf -force
  set gen_dcp_suffix ""
}

if { "write_verilog_timesim" in $options_args } {
  write_verilog -mode timesim -file ${output_name}_routed_timesim.v -force
  set gen_dcp_suffix ""
}

if { "write_bitstream" in $options_args } {
  set_property SEVERITY {Warning} [get_drc_checks]
  set_param bitgen.writeReport true
  catch { write_bitstream ${output_name}.bit -force -mask_file } error_message
  if { $error_message != "" } {
    puts "#write_bitstream_failed: $error_message"
  }
  set gen_dcp_suffix ""
}

