/*
 * Copyright (c) 2026 Dmitrii Fedorov
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Ternara tau seed: streaming int8 x ternary MAC with a saturating 16-bit
// accumulator. Pins and protocol are in docs/info.md.
module tt_um_ternara_tau_seed (
    input  wire [7:0] ui_in,    // DATA
    output wire [7:0] uo_out,   // Q
    input  wire [7:0] uio_in,   // [0] STROBE, [3:1] CMD, [4] BSEL
    output wire [7:0] uio_out,  // [5] SAT, [6] SPTR0, [7] ACK
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  // Number of stored ternary weights: 32, 16 or 0.
  localparam integer NW = 32;

  // STROBE synchroniser. p holds the previous s2. Reset puts all three at 1,
  // so a STROBE that is high through reset and after it never fires.
  reg s1;
  reg s2;
  reg p;

  always @(posedge clk) begin
    if (!rst_n) begin
      s1 <= 1'b1;
      s2 <= 1'b1;
      p  <= 1'b1;
    end else begin
      s1 <= uio_in[0];
      s2 <= s1;
      p  <= s2;
    end
  end

  // One-clock execute pulse. CMD and DATA are sampled only when fire = 1.
  wire fire = s2 & ~p;

  wire [15:0] acc;
  wire        sat;
  wire        sptr0;
  wire        ack;

  tau_seed_core #(
      .NW(NW)
  ) u_core (
      .clk  (clk),
      .rst_n(rst_n),
      .fire (fire),
      .cmd  (uio_in[3:1]),
      .data (ui_in),
      .acc  (acc),
      .sat  (sat),
      .sptr0(sptr0),
      .ack  (ack)
  );

  assign uo_out  = uio_in[4] ? acc[15:8] : acc[7:0];
  assign uio_out = {ack, sptr0, sat, 5'b00000};
  assign uio_oe  = 8'b1110_0000;

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, uio_in[7:5], 1'b0};

endmodule
