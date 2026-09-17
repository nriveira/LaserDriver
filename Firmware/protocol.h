#ifndef PROTOCOL_H
#define PROTOCOL_H

#include <stdint.h>

/*
 * LaserHat binary wire protocol — firmware (MCU) side.
 * Mirror of Pi/protocol.py; keep them in sync.
 *
 * Frame: SYNC(2: DE AD) | TYPE(1) | payload (length implied by TYPE)
 *
 * Magic-word framing — scan for SYNC, read TYPE, read the fixed payload
 * length TYPE implies.  No length field, no byte-stuffing, no CRC:
 *
 *   - Every command is answered with RSP_STATUS ("status-as-ack"), so the
 *     host confirms end-to-end that the MCU holds the values it sent.
 *   - The MCU's only inbound payload is CMD_CONFIG (bounded i/r/h), and the
 *     host guarantees a CONFIG payload never contains the SYNC bytes, so the
 *     MCU resyncs exactly on the next SYNC without a CRC.
 */

/* SYNC bytes (both >= 0x99 so they can only collide with the low 16 bits of
 * ramp/hold — which the host nudges away). */
#define PROTO_SYNC0   0xDEu
#define PROTO_SYNC1   0xADu

/* Host -> MCU (commands). */
#define CMD_CONFIG        0x01u   /* i u16, r u32, h u32 */
#define CMD_TRIGGER       0x02u   /* (no payload) */
#define CMD_QUERY         0x03u   /* (no payload) */
#define CMD_SET_MODE      0x04u   /* mode u8 (MODE_LASER / MODE_ESTIM) */
#define CMD_ESTIM_CONFIG  0x05u   /* pulse_dur_ticks u32, ipi_ticks u32 */
#define CMD_TRAIN_CONFIG  0x06u   /* count u16 (0 = until abort), period_ms u32 */
#define CMD_ABORT         0x07u   /* (no payload) stop the running pulse / train */

/* MCU -> Host (responses + events). */
#define RSP_STATUS        0x81u   /* i u16, r u32, h u32, btn u8, phase u8, tick u32,
                                     mode u8, estim_dur u32, estim_ipi u32,
                                     train_count u16, train_period_ms u32,
                                     train_done u16 */
#define EVT_PULSE_START   0x82u   /* tick u32 */
#define EVT_PULSE_END     0x83u   /* tick u32 */
#define EVT_BUTTON        0x84u   /* mask u8, edges u8 */
#define EVT_TRAIN_END     0x85u   /* tick u32 — machine back to WAITING after a
                                     train (completed or aborted) */

/* Phase byte values in RSP_STATUS (ASCII 'W' / 'T' / 'G').
 * G = in the gap between pulses of a train (outputs safe, train active). */
#define PHASE_WAITING     0x57u
#define PHASE_TRIGGERED   0x54u
#define PHASE_TRAIN_GAP   0x47u

/* Mode byte values. */
#define MODE_LASER        0x00u
#define MODE_ESTIM        0x01u

/* EStim parameter limits (100 kHz ticks: 1 tick = 10 µs). */
#define ESTIM_DUR_DEFAULT   10u     /* 100 µs */
#define ESTIM_IPI_DEFAULT   10u     /* 100 µs */
#define ESTIM_DUR_MIN        1u     /* 10 µs */
#define ESTIM_DUR_MAX     1000u     /* 10 ms */
#define ESTIM_IPI_MIN        1u
#define ESTIM_IPI_MAX     1000u

/* Pulse-train parameters.  A trigger fires `count` pulses (each with the
 * pulse config live at its start), spaced `period_ms` between pulse
 * *starts*; count 0 = repeat until CMD_ABORT.  If the period is shorter
 * than a pulse, the next pulse starts right after the previous ends.
 * period is in milliseconds (not ticks) so an hour fits while keeping the
 * top payload byte < 0xAD — only the low 16 bits can hold the SYNC word,
 * which the host nudges away exactly as for ramp/hold. */
#define TRAIN_COUNT_DEFAULT      1u
#define TRAIN_COUNT_MIN          0u     /* 0 = until abort */
#define TRAIN_COUNT_MAX      10000u
#define TRAIN_PERIOD_DEFAULT  1000u     /* 1 s */
#define TRAIN_PERIOD_MIN        10u     /* 10 ms (100 Hz) */
#define TRAIN_PERIOD_MAX   3600000u     /* 1 h */

/* Inbound (command) payload lengths. */
#define CMD_CONFIG_LEN         10u  /* i(2) + r(4) + h(4) */
#define CMD_SET_MODE_LEN        1u  /* mode(1) */
#define CMD_ESTIM_CONFIG_LEN    8u  /* pulse_dur(4) + ipi(4) */
#define CMD_TRAIN_CONFIG_LEN    6u  /* count(2) + period_ms(4) */

#define PROTO_STATUS_LEN    33u

/* Largest payload (inbound or outbound) we assemble or accept. */
#define PROTO_MAX_PAYLOAD   33u

/*
 * Payload structs.  Packed so the wire bytes are exactly the fields in
 * order with no padding, and little-endian — which matches both ends (ARM
 * Cortex-M0+ and the Pi host are LE) and Python's struct formats below.
 * That lets the firmware fill/read a struct and memcpy/cast it to the wire
 * instead of packing bytes by hand.  Reading a received buffer through a
 * packed struct is safe on M0+ (which faults on unaligned access): the
 * compiler emits byte-wise access for the misaligned members.
 *
 * The _Static_asserts lock these to the lengths above; keep them in sync
 * with Pi/protocol.py (_CONFIG = "<HII", _STATUS = "<HIIBBIBIIHIH").
 */
typedef struct __attribute__((packed)) {
    uint16_t intensity;
    uint32_t ramp_ticks;
    uint32_t hold_ticks;
} ConfigPayload;

typedef struct __attribute__((packed)) {
    uint32_t pulse_dur_ticks;
    uint32_t ipi_ticks;
} EstimConfigPayload;

typedef struct __attribute__((packed)) {
    uint16_t count;           /* pulses per trigger; 0 = until CMD_ABORT */
    uint32_t period_ms;       /* pulse-start to pulse-start */
} TrainConfigPayload;

typedef struct __attribute__((packed)) {
    uint16_t intensity;
    uint32_t ramp_ticks;
    uint32_t hold_ticks;
    uint8_t  button_mask;
    uint8_t  phase;           /* PHASE_WAITING / PHASE_TRIGGERED / PHASE_TRAIN_GAP */
    uint32_t tick;
    uint8_t  mode;            /* MODE_LASER / MODE_ESTIM */
    uint32_t estim_dur_ticks;
    uint32_t estim_ipi_ticks;
    uint16_t train_count;     /* live config */
    uint32_t train_period_ms; /* live config */
    uint16_t train_done;      /* pulses started in the current / last train */
} StatusPayload;

_Static_assert(sizeof(ConfigPayload)      == CMD_CONFIG_LEN,      "ConfigPayload layout drift");
_Static_assert(sizeof(EstimConfigPayload) == CMD_ESTIM_CONFIG_LEN, "EstimConfigPayload layout drift");
_Static_assert(sizeof(TrainConfigPayload) == CMD_TRAIN_CONFIG_LEN, "TrainConfigPayload layout drift");
_Static_assert(sizeof(StatusPayload)      == PROTO_STATUS_LEN,     "StatusPayload layout drift");

#endif /* PROTOCOL_H */
