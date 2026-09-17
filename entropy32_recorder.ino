// Entropy32 — D2 raw edge capture (pilot sketch)
//
// Temporarily replaces normal Entropy32 firmware. Does NOT run the
// 200us filter, interval pairing, or SHA-256 conditioning — it only
// timestamps every rising edge on D2 (post-LM393) and streams it out
// over serial as CSV. Reflash your normal firmware when done.
//
// Board: same ATmega328P, same D2 pull-down/comparator wiring as
// production Entropy32. Just different code.

#define D2_PIN 2
#define RING_SIZE 256   // must be a power of 2

volatile uint32_t timestamps[RING_SIZE];
volatile uint8_t head = 0;
volatile uint8_t tail = 0;
volatile bool overrun = false;

void onEdge() {
  uint8_t next_head = (uint8_t)(head + 1) & (RING_SIZE - 1);
  if (next_head == tail) {
    overrun = true;          // ring buffer full — an edge was dropped
    return;
  }
  timestamps[head] = micros();
  head = next_head;
}

uint32_t printed_index = 0;

void setup() {
  Serial.begin(115200);
  pinMode(D2_PIN, INPUT);    // board's existing pull-down handles bias
  attachInterrupt(digitalPinToInterrupt(D2_PIN), onEdge, RISING);
  Serial.println(F("edge_index,timestamp_us"));
}

void loop() {
  while (tail != head) {
    uint32_t ts = timestamps[tail];
    tail = (uint8_t)(tail + 1) & (RING_SIZE - 1);
    Serial.print(printed_index);
    Serial.print(',');
    Serial.println(ts);
    printed_index++;
  }
  if (overrun) {
    Serial.println(F("OVERRUN_DETECTED"));
    overrun = false;   // this run is invalid per the evidence protocol —
                        // note it and restart the capture
  }
}
