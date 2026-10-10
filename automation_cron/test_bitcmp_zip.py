"""Exercise zipped golden comparisons with Tcl and simulated external commands."""
import tempfile
import tkinter
import unittest
from pathlib import Path


class BitCmpZipTests(unittest.TestCase):
    def test_zip_comparison_and_cleanup(self):
        tcl = tkinter.Tcl()
        tcl.call('source', str(Path(__file__).resolve().parents[1] / 'run_proc.tcl'))
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as root:
            tcl.setvar('test_root', root)
            tcl.eval('''
                proc getCaseHome {} {return $::test_root}
                rename pwd original_pwd
                proc pwd {} {return $::test_root}
                proc bit_cmp {golden output mismatch} {
                    set ::compared $golden
                    if {[file exists $golden]} {
                        set f [open $golden rb]
                        set ::payload [read $f]
                        close $f
                    }
                }
                rename exec original_exec
                proc exec {args} {
                    if {$::unzip_fail} {error "bad archive"}
                    puts -nonewline [lindex $args end] "BITDATA"
                }
            ''')
            tcl.eval('close [open [file join $test_root golden.bit.zip] w]')
            tcl.setvar('unzip_fail', 0)
            tcl.eval('bitCmp')
            self.assertEqual(Path(str(tcl.getvar('compared'))), Path(root) / 'golden.bit')
            self.assertEqual(tcl.getvar('payload'), b'BITDATA')
            self.assertFalse(Path(str(tcl.getvar('compared'))).exists())
            tcl.setvar('unzip_fail', 1)
            tcl.setvar('compared', '')
            with self.assertRaises(tkinter.TclError):
                tcl.eval('bitCmp')
            self.assertEqual(tcl.getvar('compared'), '')
            self.assertFalse((Path(root) / 'golden.bit').exists())

    def test_bgn_zip_comparison_and_cleanup(self):
        tcl = tkinter.Tcl()
        tcl.call('source', str(Path(__file__).resolve().parents[1] / 'run_proc.tcl'))
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as root:
            tcl.setvar('test_root', root)
            tcl.eval('''
                proc getCaseHome {} {return $::test_root}
                rename pwd original_pwd
                proc pwd {} {return $::test_root}
                proc transform_bgn {source destination} {
                    if {$destination eq "golden_cmp.bgn"} {
                        set ::compared $source
                        set f [open $source rb]
                        set ::payload [read $f]
                        close $f
                    }
                }
                rename exec original_exec
                proc exec {args} {
                    if {[lindex $args 0] eq "unzip"} {
                        if {$::unzip_fail} {error "bad archive"}
                        puts -nonewline [lindex $args end] "BGNDATA"
                    } else {
                        set ::diff_called 1
                    }
                }
            ''')
            with self.assertRaisesRegex(tkinter.TclError, 'golden.bgn.zip'):
                tcl.eval('bgnCmp')
            self.assertFalse((Path(root) / 'golden.bgn').exists())

            tcl.eval('close [open [file join $test_root golden.bgn.zip] w]')
            tcl.setvar('unzip_fail', 0)
            tcl.eval('bgnCmp')
            self.assertEqual(Path(str(tcl.getvar('compared'))), Path(root) / 'golden.bgn')
            self.assertEqual(tcl.getvar('payload'), b'BGNDATA')
            self.assertEqual(str(tcl.getvar('diff_called')), '1')
            self.assertFalse((Path(root) / 'golden.bgn').exists())

            tcl.setvar('unzip_fail', 1)
            tcl.setvar('diff_called', 0)
            with self.assertRaises(tkinter.TclError):
                tcl.eval('bgnCmp')
            self.assertEqual(str(tcl.getvar('diff_called')), '0')
            self.assertFalse((Path(root) / 'golden.bgn').exists())


if __name__ == '__main__':
    unittest.main()
