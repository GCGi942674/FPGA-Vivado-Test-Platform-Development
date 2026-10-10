set is_gen 1

############ function ###############

proc move_file { source_file dest_file } {
  exec mv $source_file $dest_file
}

############### source run_proc.tcl ###########
source ${CASE_HOME}/run_proc.tcl

########### move&&check success #######

# write_checkpoint & move
if { ${gen_dcp_suffix} != "" } {
  if { $gen_dcp_suffix == "_synth" } {  # for unsynth
    set gen_dcp_suffix ""
  }

  set gen_dcp_file ${output_name}.dcp
  write_checkpoint ${gen_dcp_file} -force
  set dcp_file ${testcase_dir}/golden${gen_dcp_suffix}_enc.dcp
  move_file $gen_dcp_file $dcp_file
  if {![file exists $dcp_file]} {
    puts "#File dcp not exists. File Path: ${dcp_file}"
    exec rm ${output_name}.log -rf
    exit 1
  }
  if { $option_init != "" } {
    exec rm ${output_name}.log -rf
    exit 1
  }
}

if { "place_design" in $options_args || "place_design_from_syn" in $options_args } {
  move_file ${output_name}.log ${testcase_dir}/golden${gen_dcp_suffix}.log
} elseif { "route_design" in $options_args || "route_design_from_place" in $options_args } {
  move_file ${output_name}.log ${testcase_dir}/golden${gen_dcp_suffix}.log
} elseif { "opt_design" in $options_args || "phys_opt_design" in $options_args } {
  move_file ${output_name}.log ${testcase_dir}/golden${gen_dcp_suffix}.log
} else {
  exec rm ${output_name}.log -rf
  if { "report_utilization_from_place" in $options_args } {
    move_file ${output_name}_utilization_placed.rpt ${testcase_dir}/golden_utilization_placed.rpt
  } elseif { "report_timing_summary" in $options_args } {
    move_file ${output_name}_timing.rpt ${testcase_dir}/golden_timing.rpt
    move_file ${output_name}_timing.rpx ${testcase_dir}/golden_timing.rpx
  } elseif { "write_sdf" in $options_args } {
    move_file ${output_name}_routed.sdf ${testcase_dir}/golden_routed.sdf
  } elseif { "write_verilog_timesim" in $options_args } {
    move_file ${output_name}_routed_timesim.v ${testcase_dir}/golden_routed_timesim.v
  } elseif { "write_bitstream" in $options_args } {
    if {[file exists ${output_name}.bit]} {
      move_file ${output_name}.bit ${testcase_dir}/golden.bit
      move_file ${output_name}.bgn ${testcase_dir}/golden.bgn
      move_file ${output_name}.msk ${testcase_dir}/golden.msk
    } else {
      exec touch ${testcase_dir}/write_bitstream_failed
    }
  }
}