/*
 * Copyright (c) 2026 Dmitrii Fedorov
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Ternara tau seed core: a streaming int8 x ternary MAC with a saturating
// 16-bit accumulator, plus NW stored ternary weights (docs/info.md, How it works).
//
// NW is 32, 16 or 0. Any other value fails elaboration (g_bad below).
// With NW = 0 there is no weight memory and no pointer: STEP and LOAD_W act
// as NOP (ACK still toggles) and SPTR0 is constant 1.
//
// `fire` is one clock wide. cmd and data are used only when fire = 1.
module tau_seed_core #(
    parameter integer NW = 32
) (
    input  wire        clk,
    input  wire        rst_n,  // active low, synchronous
    input  wire        fire,   // execute the command on this clock edge
    input  wire [2:0]  cmd,
    input  wire [7:0]  data,
    output reg  [15:0] acc,    // signed 16-bit accumulator
    output reg         sat,    // sticky saturation flag
    output wire        sptr0,  // 1 when the stored-weight read pointer is 0
    output reg         ack     // toggles once per executed command
);

  // ---------------------------------------------------------------------
  // Command decode (docs/info.md, Commands). Every strobe includes fire.
  // ---------------------------------------------------------------------
  wire cmd_clear = fire & (cmd == 3'd1);
  wire cmd_load  = fire & (cmd == 3'd2);
  wire cmd_step  = fire & (cmd == 3'd3);
  wire cmd_pos   = fire & (cmd == 3'd4);
  wire cmd_neg   = fire & (cmd == 3'd5);

  // ---------------------------------------------------------------------
  // Stored weights: wcode is the raw 2-bit code at the read pointer.
  // ---------------------------------------------------------------------
  wire [1:0] wcode;

  generate
    if (NW == 32 || NW == 16) begin : g_mem
      localparam integer SW = (NW == 32) ? 5 : 4;  // width of sptr
      localparam integer LW = SW - 2;              // width of lptr
      localparam integer NB = NW / 4;              // bytes of weights

      // Weight i lives in wbits[2*i +: 2]. LOAD_W byte j holds weights
      // 4*j .. 4*j+3, weight 4*j+k in data[2*k +: 2].
      reg [2*NW-1:0] wbits;
      reg [SW-1:0]   sptr;
      reg [LW-1:0]   lptr;
      integer        j;

      always @(posedge clk) begin
        if (!rst_n) begin
          wbits <= {(2 * NW) {1'b0}};
        end else if (cmd_load) begin
          for (j = 0; j < NB; j = j + 1) begin
            if (lptr == j[LW-1:0]) wbits[8*j +: 8] <= data;
          end
        end
      end

      // Read pointer: advances on every STEP (also on weight 0) and wraps
      // naturally modulo NW.
      always @(posedge clk) begin
        if (!rst_n) begin
          sptr <= {SW{1'b0}};
        end else if (cmd_clear) begin
          sptr <= {SW{1'b0}};
        end else if (cmd_step) begin
          sptr <= sptr + {{(SW - 1) {1'b0}}, 1'b1};
        end
      end

      // Load pointer: advances on every LOAD_W and wraps modulo NW/4.
      always @(posedge clk) begin
        if (!rst_n) begin
          lptr <= {LW{1'b0}};
        end else if (cmd_clear) begin
          lptr <= {LW{1'b0}};
        end else if (cmd_load) begin
          lptr <= lptr + {{(LW - 1) {1'b0}}, 1'b1};
        end
      end

      assign wcode = wbits[{sptr, 1'b0} +: 2];
      assign sptr0 = (sptr == {SW{1'b0}});
    end else if (NW == 0) begin : g_nomem
      assign wcode = 2'b00;
      assign sptr0 = 1'b1;
      wire _unused_nw0 = &{1'b0, cmd_load};
    end else begin : g_bad
      // NW is not 32, 16 or 0: this module does not exist, so elaboration fails.
      tau_seed_invalid_NW u_bad ();
    end
  endgenerate

  // ---------------------------------------------------------------------
  // Weight decode: 00 -> 0, 01 -> +1, 11 -> -1, 10 (reserved) -> 0.
  // ---------------------------------------------------------------------
  wire st_pos = (wcode == 2'b01);
  wire st_neg = (wcode == 2'b11);

  wire wpos = cmd_pos | (cmd_step & st_pos);
  wire wneg = cmd_neg | (cmd_step & st_neg);

  // ---------------------------------------------------------------------
  // MAC: s = acc + w*x in 17 bits, exact for s in [-32896, 32895].
  // ---------------------------------------------------------------------
  wire [16:0] a17 = {acc[15], acc};
  wire [16:0] x17 = {{9{data[7]}}, data};
  wire [16:0] s   = wneg ? a17 - x17 : (wpos ? a17 + x17 : a17);
  wire        ovf = s[16] ^ s[15];

  always @(posedge clk) begin
    if (!rst_n) begin
      acc <= 16'h0000;
      sat <= 1'b0;
    end else if (cmd_clear) begin
      acc <= 16'h0000;
      sat <= 1'b0;
    end else if (wpos | wneg) begin
      acc <= ovf ? (s[16] ? 16'h8000 : 16'h7FFF) : s[15:0];
      sat <= sat | ovf;
    end
  end

  // ---------------------------------------------------------------------
  // ACK toggles on every executed command, including NOP and reserved codes.
  // ---------------------------------------------------------------------
  always @(posedge clk) begin
    if (!rst_n) begin
      ack <= 1'b0;
    end else if (fire) begin
      ack <= ~ack;
    end
  end

endmodule
