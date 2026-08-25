source ../../../run_proc.tcl
set full_path [file normalize [info script]]
set path_list [file split $full_path]
dict set params "design_name" [lindex $path_list end-1]
dict set params "device_name" [lindex $path_list end-2]
dict set params "family_name" [lindex $path_list end-3]
argParser $argc $argv
runFlow