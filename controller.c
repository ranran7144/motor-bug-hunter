/*
 * Motor Bug Hunter: deterministic, host-executable embedded C controller.
 *
 * The input snapshot is acquired once per 10 ms step. PWM writes, state changes,
 * and safety checks complete within the same step. Mechanical coasting is not
 * a fault. Bug 0 is the reference; bugs 1..30 enable one mutation each.
 *
 * This program deliberately does not claim to reproduce undefined behavior.
 * Mutations 11 and 12 are explicit stale-snapshot/interleaving models of an ISR
 * race and a missing volatile/reload respectively. Their scheduling is an input.
 * Boundary fixtures seed counters using the same initialization API as reset.
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

enum State { OFF, STARTING, RUNNING, STOPPING, FAULT, EMERGENCY_STOP };
enum Fault {
    NO_FAULT, OVERCURRENT, OVERTEMP, COMMUNICATION, SENSOR_INVALID,
    LIMIT_REACHED, ENCODER_JUMP, START_TIMEOUT, STALL
};

enum {
    CURRENT_LIMIT = 120,
    TEMPERATURE_LIMIT = 90,
    START_MIN_TICKS = 5,
    START_TIMEOUT_TICKS = 20,
    STOP_MIN_TICKS = 5,
    STALL_TICKS = 5,
    PWM_STEP = 10,
    MAX_ENCODER_STEP = 500
};

typedef struct {
    int32_t start_switch;
    int32_t stop_switch;
    int32_t emergency_stop;
    int32_t motor_current;
    int32_t motor_speed;
    int32_t encoder_position;
    int32_t limit_switch;
    int32_t temperature;
    int32_t communication_alive;
    int32_t reset_fault;
    int32_t sensor_valid;
    int32_t target_pwm;
    int32_t watchdog_due;
    int32_t isr_window;
} Input;

typedef struct {
    uint32_t cycle;
    int32_t state;
    int32_t pwm;
    int32_t brake;
    int32_t estimated_speed;
    uint32_t start_elapsed;
    uint32_t stop_elapsed;
    uint32_t stall_count;
    uint32_t start_count;
    int32_t fault_code;
} Output;

typedef struct {
    int32_t bug;
    uint32_t tick;
    int32_t state;
    int32_t pwm;
    int32_t brake;
    int32_t fault_code;
    uint32_t start_tick;
    uint32_t stop_tick;
    uint32_t stall_count;
    uint32_t start_count;
    int32_t previous_start;
    int32_t previous_estop;
    int32_t previous_comm;
    int32_t initial_comm;
    int32_t last_current;
    int32_t last_speed;
    int32_t last_temperature;
    uint16_t previous_encoder;
    int32_t encoder_initialized;
    int32_t estimated_speed;
    int32_t fault_inhibit;
    int32_t first_step;
} Controller;

static int enabled(const Controller *c, int bug)
{
    return c->bug == bug;
}

static int clamp_pwm(int requested)
{
    if (requested < 0) return 0;
    if (requested > 100) return 100;
    return requested;
}

static uint32_t start_elapsed(const Controller *c)
{
    /* M03: mixing a truncated clock with a full-width timestamp. */
    if (enabled(c, 3)) return (uint32_t)(uint16_t)c->tick - c->start_tick;
    return c->tick - c->start_tick;
}

static void enter_fault(Controller *c, int code)
{
    c->state = FAULT;
    c->fault_code = code;
    c->fault_inhibit = 1;
    c->pwm = 0;
    c->brake = 1;
}

static void enter_estop(Controller *c)
{
    c->state = EMERGENCY_STOP;
    c->pwm = 0;
    c->brake = 1;
}

static void release_latch(Controller *c)
{
    c->state = OFF;
    c->fault_code = NO_FAULT;
    c->stall_count = 0;
    c->pwm = 0;
    c->brake = 0;
    /* M10: a hidden inhibit survives the otherwise successful reset. */
    if (!enabled(c, 10)) c->fault_inhibit = 0;
}

static void begin_start(Controller *c)
{
    ++c->start_count;
    /* M15: the 1000th accepted start is lost. */
    if (enabled(c, 15) && c->start_count % 1000u == 0u) return;
    if (c->fault_inhibit) return;
    c->state = STARTING;
    c->start_tick = c->tick;
    c->stall_count = 0;
    c->brake = 0;
}

static void begin_stop(Controller *c)
{
    c->state = STOPPING;
    /* M04: the previous stop timestamp is accidentally reused. */
    if (!enabled(c, 4)) c->stop_tick = c->tick;
    c->pwm = 0;
    c->brake = 1;
}

static void update_pwm(Controller *c, int requested)
{
    int target = clamp_pwm(requested);
    int step = PWM_STEP;

    /* M22: bypassing saturation can expose a PWM above 100 percent. */
    if (enabled(c, 22)) target = requested;
    /* M29: a unit conversion error quadruples the per-tick slew limit. */
    if (enabled(c, 29)) step = 40;

    if (c->pwm < target) {
        c->pwm += step;
        if (c->pwm > target) c->pwm = target;
    } else if (c->pwm > target) {
        /* M28: unsigned subtraction wraps when PWM is below the step. */
        if (enabled(c, 28)) {
            c->pwm = (uint8_t)(c->pwm - step);
            if (c->pwm < target) c->pwm = target;
        } else {
            c->pwm -= step;
            if (c->pwm < target) c->pwm = target;
        }
    }
}

static int update_encoder(Controller *c, const Input *in)
{
    uint16_t current = (uint16_t)in->encoder_position;
    int32_t delta = 0;
    if (c->encoder_initialized) {
        uint16_t wrapped = (uint16_t)(current - c->previous_encoder);
        delta = wrapped <= 32767u ? (int32_t)wrapped : (int32_t)wrapped - 65536;
        /* M09: subtracting promoted values before wrap normalization. */
        if (enabled(c, 9)) delta = (int32_t)current - (int32_t)c->previous_encoder;
    }
    c->encoder_initialized = 1;
    c->previous_encoder = current;
    c->estimated_speed = delta;
    return delta > MAX_ENCODER_STEP || delta < -MAX_ENCODER_STEP;
}

static int physical_fault(const Controller *c, const Input *in, int encoder_bad)
{
    int current = in->motor_current;
    int current_trip;
    int temp_trip;

    /* M21: a narrowing conversion hides overcurrent at values >= 256. */
    if (enabled(c, 21)) current = (uint8_t)current;
    /* M02 and M18: equality at the specified limit is missed. */
    current_trip = enabled(c, 2) ? current > CURRENT_LIMIT : current >= CURRENT_LIMIT;
    temp_trip = enabled(c, 18) ? in->temperature > TEMPERATURE_LIMIT : in->temperature >= TEMPERATURE_LIMIT;

    if (!in->sensor_valid) return SENSOR_INVALID;
    if (!in->communication_alive) return COMMUNICATION;
    if (current_trip) return OVERCURRENT;
    if (temp_trip) return OVERTEMP;
    /* M17: the limit input is omitted only in the running state. */
    if (in->limit_switch && !(enabled(c, 17) && c->state == RUNNING)) return LIMIT_REACHED;
    if (encoder_bad) return ENCODER_JUMP;
    return NO_FAULT;
}

EXPORT void *motor_create(int bug, uint32_t initial_tick, uint32_t initial_start_count)
{
    Controller *c = (Controller *)calloc(1, sizeof(Controller));
    if (!c) return NULL;
    c->bug = bug;
    c->tick = initial_tick;
    c->start_count = initial_start_count;
    c->state = OFF;
    c->previous_comm = 1;
    c->initial_comm = 1;
    c->first_step = 1;
    return c;
}

EXPORT void motor_destroy(void *handle)
{
    free(handle);
}

EXPORT int motor_input_size(void) { return (int)sizeof(Input); }
EXPORT int motor_output_size(void) { return (int)sizeof(Output); }

EXPORT void motor_step(void *handle, const Input *raw, Output *out)
{
    Controller *c = (Controller *)handle;
    Input in = *raw;
    int previous_pwm = c->pwm;
    int previous_state = c->state;
    int rising_start;
    int encoder_bad;
    int code;
    int allow_reset;
    int delay_estop_output = 0;
    int delay_comm_output = 0;
    int watchdog_stale_output = 0;

    /* M06: invalid sensor frames are silently replaced by old data. */
    if (enabled(c, 6) && !in.sensor_valid) {
        in.sensor_valid = 1;
        in.motor_current = c->last_current;
        in.motor_speed = c->last_speed;
        in.temperature = c->last_temperature;
    }

    /* M11: deterministic ISR/main interleaving model (no C data race). */
    if (enabled(c, 11) && in.isr_window) in.emergency_stop = c->previous_estop;

    /* M12: deterministic missing-reload model (no compiler/UB claim). */
    if (enabled(c, 12)) in.communication_alive = c->initial_comm;

    rising_start = in.start_switch && !c->previous_start;
    encoder_bad = update_encoder(c, &in);
    code = physical_fault(c, &in, encoder_bad);
    allow_reset = in.reset_fault && in.motor_speed <= 5;

    /* M27: reset ignores the standstill precondition. */
    if (enabled(c, 27)) allow_reset = in.reset_fault;

    if (in.emergency_stop) {
        delay_estop_output = enabled(c, 1) && !c->previous_estop;
        watchdog_stale_output = enabled(c, 14) && in.watchdog_due;
        enter_estop(c);
        /* M26: reset is allowed to clear an actively asserted E-STOP. */
        if (enabled(c, 26) && allow_reset) release_latch(c);
    } else if (code != NO_FAULT) {
        delay_comm_output = enabled(c, 7) && code == COMMUNICATION && c->previous_comm;
        enter_fault(c, code);
    } else if (c->state == FAULT || c->state == EMERGENCY_STOP) {
        /* M25: reconnection clears a latched communication fault. */
        if (enabled(c, 25) && c->state == FAULT && c->fault_code == COMMUNICATION) {
            release_latch(c);
            begin_start(c);
        } else if (allow_reset) {
            release_latch(c);
        }
    } else {
        switch (c->state) {
        case OFF:
            c->pwm = 0;
            c->brake = 0;
            /* M16: simultaneous STOP/START wrongly gives START priority. */
            if ((rising_start || (enabled(c, 24) && in.start_switch)) &&
                (!in.stop_switch || enabled(c, 16))) {
                begin_start(c);
            }
            break;

        case STARTING: {
            uint32_t elapsed = start_elapsed(c);
            int ready;
            int timed_out;
            if (in.stop_switch) {
                begin_stop(c);
                break;
            }
            ready = elapsed >= START_MIN_TICKS && in.motor_speed >= 30;
            /* M05: OR admits an unqualified startup. */
            if (enabled(c, 5)) ready = elapsed >= START_MIN_TICKS || in.motor_speed >= 30;
            timed_out = elapsed >= START_TIMEOUT_TICKS;
            /* M19: startup times out one tick late at the boundary. */
            if (enabled(c, 19)) timed_out = elapsed > START_TIMEOUT_TICKS;
            if (ready) c->state = RUNNING;
            else if (timed_out) enter_fault(c, START_TIMEOUT);
            break;
        }

        case RUNNING:
            if (in.stop_switch) {
                begin_stop(c);
                break;
            }
            if (in.motor_speed < 5) ++c->stall_count;
            /* M20: nonconsecutive low-speed samples accumulate. */
            else if (!enabled(c, 20)) c->stall_count = 0;
            if (c->stall_count >= STALL_TICKS) enter_fault(c, STALL);
            break;

        case STOPPING:
            c->pwm = 0;
            c->brake = 1;
            /* M08: a quick new START aborts the mandatory stopping phase. */
            if (enabled(c, 8) && rising_start && !in.stop_switch) {
                begin_start(c);
            } else if (c->tick - c->stop_tick >= STOP_MIN_TICKS && in.motor_speed <= 5) {
                c->state = OFF;
                /* M23: the brake remains applied after the OFF transition. */
                if (!enabled(c, 23)) c->brake = 0;
            }
            break;

        default:
            break;
        }
    }

    if (c->state == STARTING || c->state == RUNNING) update_pwm(c, in.target_pwm);
    else c->pwm = 0;

    if (delay_estop_output || delay_comm_output || watchdog_stale_output) c->pwm = previous_pwm;

    /* M13: power-on initialization leaves the PWM register nonzero. */
    if (enabled(c, 13) && c->first_step && c->state == OFF) c->pwm = 10;

    c->previous_start = raw->start_switch;
    /* M30: reset drops edge history, allowing a held START to rearm. */
    if (enabled(c, 30) && previous_state == FAULT && c->state == OFF) c->previous_start = 0;
    c->previous_estop = raw->emergency_stop;
    c->previous_comm = raw->communication_alive;
    if (raw->sensor_valid) {
        c->last_current = raw->motor_current;
        c->last_speed = raw->motor_speed;
        c->last_temperature = raw->temperature;
    }

    out->cycle = c->tick;
    out->state = c->state;
    out->pwm = c->pwm;
    out->brake = c->brake;
    out->estimated_speed = c->estimated_speed;
    out->start_elapsed = c->state == STARTING ? start_elapsed(c) : 0;
    out->stop_elapsed = c->state == STOPPING ? c->tick - c->stop_tick : 0;
    out->stall_count = c->stall_count;
    out->start_count = c->start_count;
    out->fault_code = c->fault_code;
    c->first_step = 0;
    ++c->tick;
}
