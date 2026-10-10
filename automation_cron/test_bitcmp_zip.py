"""Exercise bitCmp with Tcl; simulate the external unzip and bit_cmp commands."""
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
            tcl.eval('bitCmp')
            self.assertEqual(Path(str(tcl.getvar('compared'))), Path(root) / 'golden.bit')
            tcl.eval('close [open [file join $test_root golden.bit.zip] w]')
            tcl.setvar('unzip_fail', 0)
            tcl.eval('bitCmp')
            self.assertEqual(tcl.getvar('payload'), b'BITDATA')
            self.assertFalse(Path(str(tcl.getvar('compared'))).exists())
            tcl.setvar('unzip_fail', 1)
            tcl.setvar('compared', '')
            with self.assertRaises(tkinter.TclError):
                tcl.eval('bitCmp')
            self.assertEqual(tcl.getvar('compared'), '')
            self.assertEqual(list(Path(root).glob('golden_cmp.*')), [])


if __name__ == '__main__':
    unittest.main()
