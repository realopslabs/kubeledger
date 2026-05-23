#!/usr/bin/env python
__author__ = "Rodrigue Chakode"
__copyright__ = "Copyright 2019 Rodrigue Chakode and contributors"
__credits__ = ["Rodrigue Chakode and contributors"]
__license__ = "Apache"
__version__ = "2.0"
__maintainer__ = "Rodrigue Chakode"
__email__ = "Rodrigue Chakode <rodrigue.chakode @ gmail dot com"
__status__ = "Production"

import backend


class TestDecodeK8sMetrics(object):
    def test_decode_cpu_capacity_nounit(self):
        assert round(backend.K8sUsage().decode_capacity("1"), 0) == 1
        assert round(backend.K8sUsage().decode_capacity("64"), 0) == 64

    def test_decode_cpu_capacity_milli(self):
        assert round(backend.K8sUsage().decode_capacity("1m"), 3) == 1e-3
        assert round(backend.K8sUsage().decode_capacity("875m"), 3) == 875e-3

    def test_decode_cpu_capacity_kilo(self):
        assert round(backend.K8sUsage().decode_capacity("4.5k"), 3) == 4500
        assert round(backend.K8sUsage().decode_capacity("192k"), 3) == 192000

    def test_decode_cpu_capacity_nano(self):
        assert round(backend.K8sUsage().decode_capacity("1n"), 9) == 1e-9
        assert round(backend.K8sUsage().decode_capacity("875n"), 9) == 875e-9

    def test_decode_cpu_capacity_micro(self):
        assert round(backend.K8sUsage().decode_capacity("1u"), 6) == 1e-6
        assert round(backend.K8sUsage().decode_capacity("875u"), 6) == 875e-6

    def test_decode_memory_capacity_nounit(self):
        assert round(backend.K8sUsage().decode_capacity("256"), 0) == 256
        assert round(backend.K8sUsage().decode_capacity("875"), 0) == 875

    def test_decode_memory_capacity_Ki(self):
        assert round(backend.K8sUsage().decode_capacity("1Ki"), 3) == 1024
        assert round(backend.K8sUsage().decode_capacity("875Ki"), 3) == 875 * 1024
        assert round(backend.K8sUsage().decode_capacity("1K"), 3) == 1e3
        assert round(backend.K8sUsage().decode_capacity("875K"), 3) == 875 * 1e3

    def test_decode_memory_capacity_Mi(self):
        # Mi = 1073741824 = 1024 * 1024 * 1024
        assert round(backend.K8sUsage().decode_capacity("1Mi"), 6) == 1048576
        assert round(backend.K8sUsage().decode_capacity("875Mi"), 6) == 875 * 1024 * 1024
        # M = 1e6
        assert round(backend.K8sUsage().decode_capacity("1M"), 6) == 1e6
        assert round(backend.K8sUsage().decode_capacity("875M"), 6) == 875 * 1e6

    def test_decode_memory_capacity_Gi(self):
        # Gi = 1073741824 = 1024 * 1024 * 1024
        assert round(backend.K8sUsage().decode_capacity("1Gi"), 9) == 1073741824
        assert round(backend.K8sUsage().decode_capacity("875Gi"), 9) == 875 * 1024 * 1024 * 1024
        # G = 1e9
        assert round(backend.K8sUsage().decode_capacity("1G"), 9) == 1e9
        assert round(backend.K8sUsage().decode_capacity("875G"), 9) == 875 * 1e9

    def test_decode_memory_capacity_n_u_m(self):
        # m = 1e-3
        assert round(backend.K8sUsage().decode_capacity("14162554060800m"), 1) == 14162554060.8
        assert backend.K8sUsage().decode_capacity("1u") == 1e-6
        assert round(backend.K8sUsage().decode_capacity("875m"), 3) == 875 * 1e-3

    def test_decode_memory_capacity_Pi(self):
        # Pi = 1125899906842624 = 1024 * 1024 * 1024 * 1024 * 1024
        assert round(backend.K8sUsage().decode_capacity("1Pi"), 15) == 1125899906842624
        assert round(backend.K8sUsage().decode_capacity("875Pi"), 15) == 875 * 1024 * 1024 * 1024 * 1024 * 1024
        # P = 1e15
        assert round(backend.K8sUsage().decode_capacity("1P"), 9) == 1e15
        assert round(backend.K8sUsage().decode_capacity("875P"), 9) == 875 * 1e15
