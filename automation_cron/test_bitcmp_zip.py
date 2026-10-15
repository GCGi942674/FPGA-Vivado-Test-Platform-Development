"""Exercise zipped golden comparisons with Tcl and simulated external commands."""
import tempfile
import tkinter
import unittest
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'vivado_runner' / 'lib' / 'python' / 'vvdCmpFiles.py'


def bit_file(payload, metadata=b'golden'):
    data = bytes.fromhex('00090ff00ff00ff00ff0000001')
    for tag in b'abcd':
        value = metadata + b'\0'
        data += bytes([tag]) + len(value).to_bytes(2, 'big') + value
    data += b'e' + (len(payload) + 4).to_bytes(4, 'big')
    return data + bytes.fromhex('AA995566') + payload


class BitCmpZipTests(unittest.TestCase):
    def test_frame_comparison_result_file_and_errors(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as root:
            root = Path(root)
            archive = root / 'golden.bit.zip'
            output = root / 'output.bit'
            result = root / 'mis_bit.txt'
            with zipfile.ZipFile(archive, 'w') as zipped:
                zipped.writestr('golden.bit', bit_file(b'\x01\x02', b'reference'))

            def compare():
                return subprocess.run([sys.executable, str(SCRIPT), str(archive), str(output),
                                       '--bit-result', str(result)], capture_output=True, text=True)

            output.write_bytes(bit_file(b'\x01\x02', b'current'))
            self.assertEqual(compare().returncode, 0)
            self.assertEqual(result.read_bytes(), b'')

            output.write_bytes(bit_file(b'\x01\x03', b'current'))
            compared = compare()
            self.assertEqual(compared.returncode, 1, compared.stderr)
            self.assertIn('post-sync byte 0x1', result.read_text())
            two_arg = subprocess.run([sys.executable, str(SCRIPT), str(archive), str(output)],
                                     capture_output=True, text=True)
            self.assertEqual(two_arg.returncode, 1, two_arg.stderr)
            self.assertIn('Files are different', two_arg.stdout)

            output.write_bytes(b'broken bitstream')
            result.unlink()
            self.assertEqual(compare().returncode, 2)
            self.assertFalse(result.exists())

            output.write_bytes(bit_file(b'\x01\x02'))
            with zipfile.ZipFile(archive, 'w') as zipped:
                zipped.writestr('wrong.bit', bit_file(b'\x01\x02'))
            self.assertEqual(compare().returncode, 2)
            self.assertFalse(result.exists())

    def test_tcl_bit_comparison_status(self):
        tcl = tkinter.Tcl()
        tcl.call('source', str(ROOT / 'run_proc.tcl'))
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as root:
            tcl.setvar('test_root', root)
            old_dir = tcl.call('pwd')
            tcl.call('cd', root)
            tcl.eval('''
                proc getCaseHome {} {return $::test_root}
                rename exec original_exec
                proc exec {args} {
                    set ::command $args
                    if {$::exit_code == 0} {
                        return "Files are same"
                    }
                    if {$::exit_code == 1} {
                        return "Files are different\nDifferent payload bytes: 1"
                    }
                    if {$::exit_code == 3} {
                        return -code error -errorcode {CHILDSTATUS 123 1} "Files are different\nDifferent payload bytes: 1"
                    }
                    return -code error -errorcode [list CHILDSTATUS 123 $::exit_code] "Usage: vvdCmpFiles.py <file1> <file2>"
                }
            ''')
            tcl.eval('close [open [file join $test_root golden.bit.zip] w]')
            tcl.setvar('exit_code', 0)
            tcl.eval('bitCmp')
            try:
                command = tcl.getvar('command')
                self.assertEqual(Path(str(command[1])), SCRIPT)
                self.assertEqual(len(command), 4)
                self.assertEqual((Path(root) / 'mis_bit.txt').read_bytes(), b'')
                tcl.setvar('exit_code', 1)
                tcl.eval('bitCmp')
                self.assertIn('Different payload bytes', (Path(root) / 'mis_bit.txt').read_text())
                tcl.setvar('exit_code', 3)
                tcl.eval('bitCmp')
                self.assertIn('Different payload bytes', (Path(root) / 'mis_bit.txt').read_text())
                tcl.setvar('exit_code', 2)
                with self.assertRaises(tkinter.TclError):
                    tcl.eval('bitCmp')
                self.assertFalse((Path(root) / 'mis_bit.txt').exists())
            finally:
                tcl.call('cd', old_dir)

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
