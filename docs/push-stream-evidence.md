# Push stream: evidence from the panel firmware

The push decoder in `custom_components/tuxedo_touch/push.py` describes each frame layout in plain terms. This file holds the firmware evidence behind those layouts: the producer in the vendor's `/tuxedo` binary and the format strings that build each record. Wire captures and the reference reader are in `tuxedo_touch_api_notes.md`, "The push stream".

## The partition status producer

`CReceiverThread::sltSendChangedPartitionStatus` at `0x144880` in `/tuxedo` builds every partition status record.

### Only the selected partition is sent

```text
0x144884  mov  r3, #0x15            ; 21, the command id
0x1448a0  bl   GetCurrentPartition()
0x1448a4  cmp  r0, r4               ; r4 = the partition that changed
0x1448a8  beq  0x1448b4             ; equal -> build and send
0x1448ac  add  sp, sp, #0x26c       ; NOT equal -> return, send nothing
```

A frame is emitted only when the partition that changed is the panel's currently selected partition. The stream carries no partition field because the producer has already applied the filter.

### Link down is -1 in field 2

```text
0x144a7c  bl     PanelIsTalking()
0x144a80  cmp    r0, #0
0x144a84  mvneq  r3, #0          ; link down -> r3 = -1
0x144a88  streq  r3, [sp, #8]    ; -1 stored into the message field
0x144a8c  bne    0x144ae8        ; otherwise the real status code
0x144aa0  bl     osal_MqSend(int, char*, int)   ; SENT EITHER WAY
```

The frame is still sent with the last display text and state flag the Tuxedo drew before it lost the ECP link. Field 2 at `-1` is the one value that says to believe nothing else in the frame.

### Command 21 and command 22

The same function writes both. The state byte and the text are the same; the type is 22 while `GetOnlineStatus() != 1`, the VISTA's own online byte (1 online, 2 to 4 its busy, downloading and offline family). Field `+0x08` is `PanelIsTalking() ? GetOnlineStatus() : -1`. The type says whether the VISTA calls itself online, the code says whether the Tuxedo can hear it, and `-1` can arrive on either type.

Barracuda's handler for the 22 (`0xd9c4`) formats `%d%s%d%s%s%s%d` with the reply's `+0x08` as the last conversion, and follows it with two `-1` copies where a 21 gets three. tuxweb v16 reproduces both. No capture holds a 22: this panel has not been offline while anything was recording, so its shape comes from this handler.

## The console record

`bprintf("%d%s%d%s%s", session, ":", 20, ":2", text)` at `0x85304` builds the id-20 keypad LCD record. The `2` after the second colon is the second character of the constant `":2"` at `0x852f4`, never a colour digit, which is why the decoder keys on it.

## User code accept and decline

`sltSendUserCodeAcceptedMsg` posts to the queue that feeds the push stream, but a full arm/disarm cycle driven through the REST `AdvancedSecurity/ArmWithCode` endpoint emitted no accept frame. The integration confirms a command by the state flag on the stream instead.

| Fact | Source |
|---|---|
| `CUiReceiverThread::run` emits the signal on message type 11 or 13 (accept) and 9 (decline) | `/tuxedo` |
| A flag byte inside the message picks the web variant over the local one | `/tuxedo` |
| Nothing posts those message types for the REST route | `/tuxedo` |
| Every call site builds the message in memory, so the sender of 9, 11 and 13 is not statically resolvable | `/tuxedo` |
| `CKeyPadWin::sltHandleUserCodeAccepted` and `...Declined` are absent from that class's `qt_static_metacall` dispatch table while 20 sibling slots are present | `/tuxedo` |

The last row is about reception, not emission: it is evidence against the local keypad being the trigger, not proof. Which surface, if any, posts these messages is open; a live test through the virtual keypad would settle it.
