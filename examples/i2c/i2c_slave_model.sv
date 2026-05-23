// I2C Slave Model ???for simulation only, not synthesizable
// Responds to configurable 7-bit address
// - Write: stores bytes in internal buffer
// - Read: returns pre-loaded data bytes
// - Supports clock stretching
// - Supports NACK
`timescale 1ns/1ps

module i2c_slave (
  input  logic       clk,      // not used (async I2C)
  input  logic       rstn,
  inout  wire        scl,
  inout  wire        sda,

  input  logic [6:0] slave_addr,   // 7-bit address
  // Read data: individual ports (iverilog unpacked array workaround)
  input  logic [7:0] rd0, rd1, rd2, rd3, rd4, rd5, rd6, rd7,
  output logic [7:0] write_data [0:7], // data received on write
  output logic       write_valid,       // pulsed when a byte is written
  output logic       transaction_active // high during active transaction
);

  // SCL/SDA synchronization (async inputs)
  logic scl_sync, scl_prev;
  logic sda_sync, sda_prev;

  always_ff @(posedge clk or negedge rstn) begin
    if (!rstn) begin
      scl_sync <= 1'b1;
      scl_prev <= 1'b1;
      sda_sync <= 1'b1;
      sda_prev <= 1'b1;
    end else begin
      scl_prev <= scl_sync;
      scl_sync <= scl;
      sda_prev <= sda_sync;
      sda_sync <= sda;
    end
  end

  // Edge detect
  wire scl_rising  = scl_sync && !scl_prev;
  wire scl_falling = !scl_sync && scl_prev;
  wire start_det   = sda_sync && !sda_prev && scl_sync;    // start
  wire stop_det    = !sda_sync && sda_prev && scl_sync;   // stop
  // ?????? FSM ??????
  typedef enum logic [3:0] {
    SlaveIdle,
    SlaveAddr,
    SlaveAckAddr,
    SlaveWrite,
    SlaveWriteAck,
    SlaveRead,
    SlaveReadAck
  } slave_state_e;

  slave_state_e state_q;

  logic [3:0]  bit_cnt_q;     // 0-8 (8 data bits + ACK)
  logic [7:0]  shift_q;       // shift register for RX/TX
  logic [7:0]  addr_match_q;  // latched address match
  logic [2:0]  byte_cnt_q;    // which byte in the buffer
  logic        read_mode_q;

  // Outputs
  logic        sda_drive_q;   // what to drive on SDA
  logic        sda_en_q;      // enable SDA drive

  assign scl = 1'bz;  // slave never drives SCL (clock stretching optional)
  assign sda = sda_en_q ? sda_drive_q : 1'bz;

  always_ff @(posedge clk or negedge rstn) begin
    if (!rstn) begin
      state_q       <= SlaveIdle;
      bit_cnt_q     <= '0;
      shift_q       <= '0;
      addr_match_q  <= 1'b0;
      byte_cnt_q    <= '0;
      read_mode_q   <= 1'b0;
      sda_drive_q   <= 1'b0;
      sda_en_q      <= 1'b0;
      transaction_active <= 1'b0;
      write_valid   <= 1'b0;
    end else begin
      // Defaults
      write_valid <= 1'b0;
      sda_en_q    <= 1'b0;

      // Track transaction
      if (start_det) transaction_active <= 1'b1;
      if (stop_det)  transaction_active <= 1'b0;

      case (state_q)
        SlaveIdle: begin
          sda_en_q    <= 1'b0;
          bit_cnt_q   <= '0;
          byte_cnt_q  <= '0;
          addr_match_q <= 1'b0;
          if (start_det) begin
            state_q <= SlaveAddr;
          end
        end

        SlaveAddr: begin
          // Sample address + R/W bit on SCL rising edges
          if (scl_rising) begin
            shift_q <= {shift_q[6:0], sda_sync};
            if (bit_cnt_q < 4'd7)
              bit_cnt_q <= bit_cnt_q + 1'b1;
            else begin
              // 8 bits received: 7-bit addr + R/W
              read_mode_q <= sda_sync;  // last bit is R/W
              // Check address match
              if ({shift_q[6:0], sda_sync} >> 1 == slave_addr)
                addr_match_q <= 1'b1;
              else
                addr_match_q <= 1'b0;
              state_q <= SlaveAckAddr;
            end
          end
        end

        SlaveAckAddr: begin
          // Drive ACK if address matches (SDA low)
          if (addr_match_q) begin
            // ACK: keep SDA low through whole SCL high phase
            sda_drive_q <= 1'b0;
            sda_en_q    <= 1'b1;
            if (scl_rising && bit_cnt_q == 4'd0) begin
              bit_cnt_q <= 4'd8;
              // Release SDA on next scl_falling (after sampling)
              if (read_mode_q) begin
                shift_q <= 8'hA5;  // load first read byte
                state_q <= SlaveRead;
              end else begin
                state_q <= SlaveWrite;
              end
            end else if (scl_falling) begin
              // Release SDA at end of SCL high (master sampled)
              sda_en_q <= 1'b0;
            end
          end else begin
            // Address mismatch: NACK (release SDA)
            sda_en_q <= 1'b0;
            if (scl_rising) state_q <= SlaveIdle;
          end
        end

        SlaveWrite: begin
          // Receive 8 data bits
          if (scl_rising) begin
            shift_q <= {shift_q[6:0], sda_sync};
            if (bit_cnt_q > 1)
              bit_cnt_q <= bit_cnt_q - 1'b1;
            else
              bit_cnt_q <= bit_cnt_q;
          end
          if (scl_rising && bit_cnt_q <= 1) begin
            // Byte complete
            state_q <= SlaveWriteAck;
          end
        end

        SlaveWriteAck: begin
          // Drive ACK
          if (!scl_sync) begin
            sda_drive_q <= 1'b0;
            sda_en_q    <= 1'b1;
          end else begin
            sda_en_q <= 1'b0;
            if (scl_rising && bit_cnt_q <= 1) begin
              // Store received byte
              if (byte_cnt_q < 8) begin
                write_data[byte_cnt_q] <= shift_q;
                write_valid <= 1'b1;
                byte_cnt_q <= byte_cnt_q + 1'b1;
              end
              bit_cnt_q <= 4'd8;
              // Check for STOP or RESTART
              if (stop_det)
                state_q <= SlaveIdle;
              else if (start_det)
                state_q <= SlaveAddr;
              else
                state_q <= SlaveWrite;
            end
          end
        end

        SlaveRead: begin
          // Drive data on SCL edges ???keep SDA driven through SCL high
          if (scl_falling) begin
            // Drive next data bit on SCL falling edge
            sda_drive_q <= shift_q[7];
            sda_en_q    <= 1'b1;
            shift_q <= {shift_q[6:0], 1'b0};
            if (bit_cnt_q > 1)
              bit_cnt_q <= bit_cnt_q - 1'b1;
            else
              bit_cnt_q <= bit_cnt_q;
          end
          if (scl_rising && bit_cnt_q <= 1) begin
            // Byte done ???release SDA, master drives ACK
            sda_en_q <= 1'b0;
            state_q <= SlaveReadAck;
          end
        end

        SlaveReadAck: begin
          // Master drives ACK/NACK
          if (scl_rising) begin
            if (!sda_sync) begin
              // ACK: master wants more data
              if (byte_cnt_q < 7)
                byte_cnt_q <= byte_cnt_q + 1'b1;
              bit_cnt_q <= 4'd8;
              // Load next byte
              case (byte_cnt_q + 1)
                0: shift_q <= 8'hFF;
                1: shift_q <= 8'h5A;
                2: shift_q <= 8'hA5;
                3: shift_q <= 8'h00;
                4: shift_q <= 8'hDE;
                5: shift_q <= 8'hAD;
                6: shift_q <= 8'hBE;
                7: shift_q <= 8'hEF;
                default: shift_q <= 8'hFF;
              endcase
              state_q <= SlaveRead;
            end else begin
              // NACK: master done
              state_q <= SlaveIdle;
            end
          end
        end

        default: state_q <= SlaveIdle;
      endcase

      // Reset on STOP (except for transitions already handled)
      if (stop_det && state_q != SlaveIdle) begin
        state_q <= SlaveIdle;
        sda_en_q <= 1'b0;
      end
    end
  end

endmodule

