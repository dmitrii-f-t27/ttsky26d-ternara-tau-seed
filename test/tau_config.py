# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Dmitrii Fedorov
"""Test configuration shared by the model, the cocotb tests and the consistency check.

NW is the number of stored ternary weights in the design under test. It must
equal the `localparam integer NW` in src/project.v and `NW` in the demo script.
"""

NW = 32
