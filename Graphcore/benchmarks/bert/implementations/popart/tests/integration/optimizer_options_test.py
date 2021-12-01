import pytest
import sys
from pathlib import Path
from examples_tests.test_util import SubProcessChecker

bert_path = str(Path(__file__).parent.parent.parent)


class TestBuildAndRun(SubProcessChecker):

    def _get_bert_command(self, extra_args=None):
        base_cmd = ("python bert.py --config configs/squad_base_128.json "
                    "--num-layers 1 --vocab-length 9728 --no-validation --epochs 1 "
                    "--device-connection-type ondemand")
        if extra_args is not None and len(extra_args) > 0:
            base_cmd += " " + " ".join(extra_args)
        return base_cmd

    def setUp(self):
        self.run_command("make", bert_path, [])

    @pytest.mark.ipus(3)
    @pytest.mark.category2
    def test_run_no_schedule(self):
        cmd = self._get_bert_command()
        self.run_command(cmd,
                         bert_path,
                         ["Compiling Training Graph", "Compiled.", "Training Started", "Training Finished"])

    @pytest.mark.ipus(3)
    @pytest.mark.category2
    def test_run_lr_schedule(self):
        cmd = self._get_bert_command(["--lr-schedule-by-step 0:0.0001 5:0.00005"])
        self.run_command(cmd,
                         bert_path,
                         ["Compiling Training Graph", "Compiled.", "Training Started", "Training Finished"])

    @pytest.mark.ipus(3)
    @pytest.mark.category2
    def test_run_ls_schedule(self):
        cmd = self._get_bert_command(["--ls-schedule-by-step 0:20 5:30"])
        self.run_command(cmd,
                         bert_path,
                         ["Compiling Training Graph", "Compiled.", "Training Started", "Training Finished"])

    @pytest.mark.ipus(3)
    @pytest.mark.category2
    def test_run_both_schedules(self):
        cmd = self._get_bert_command(["--lr-schedule-by-step 0:0.0001 5:0.00005", "--ls-schedule-by-step 0:20 5:30"])
        self.run_command(cmd,
                         bert_path,
                         ["Compiling Training Graph", "Compiled.", "Training Started", "Training Finished"])
