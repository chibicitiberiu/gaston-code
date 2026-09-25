# Step 2b: `Connection` out of `net.cpp` (working plan)

Written 2026-09-25 from a read of `win/net/net.{h,cpp}`, `rpc/rpc.*`,
`checks/{netcheck,rpccheck}.c` and the app callers. Step 2 of `REMOTE.md`.
Line numbers are as of commit `63dd2ad`.

## What is generic and what is the client's

| Piece | Where | Goes to |
|---|---|---|
| Socket lifecycle, `recv_exact`, reader loop (length prefix, `RC_MAX_FRAME`, parse, inflate) | net.cpp:258-327 | `Connection` |
| Out queue (`rc_outq_item`, 64 KB hard cap), sender thread, `outq_*` | net.h:49-69, net.cpp:779-845 | `Connection` |
| Inflate/deflate streams, scratch, adaptive-compression stats (file statics today) | net.cpp:43-165 | `Connection` members |
| `send_frame` (budget wait, compress, build, enqueue) | net.cpp:865-1019 | `Connection::send`, except the `is_bulk` type list (client vocabulary) |
| `do_connect` | net.cpp:414-450 | `Connection::connect`; a server gets `adopt(SOCKET)` |
| Reader exit (`running=0`, FAILED/DISCONNECTED), `bad_alloc` thread wrappers | net.cpp:381-412, 849-860 | `Connection` |
| `rc_net_stop` phases 2-3 and every gated release | net.cpp:1051-1233 | `Connection::drain/signalStop/join/release` |
| `WM_RC_FRAME`/`WM_RC_STATE`, posting to an HWND, inflight gate | net.h:38-39, net.cpp:218-228, 355-378 | client: the `FrameSink` it injects |
| Routing into rpc (`rc_rpc_route`), rpc start/stop (phase 1) | net.cpp:344-353, 716, 1079-1080 | client adapter |
| HELLO (caps, sysinfo, sync root, tooldet) | net.cpp:455-706 | client adapter |
| `rc_net` fields `notify host port secret sess leaked inflight` | net.h:71-109 | client; `rc_net` stays a C-visible POD (C checks, memset in main.cpp) with an opaque `conn` |

## API sketch

```cpp
class FrameSink {                // reader thread; takes ownership of f
    virtual void onFrame(Connection &c, rc_frame *f) = 0;
    virtual void onState(Connection &c, rc_state s) = 0;   // must not block
};
class Connection {
    Connection(FrameSink *sink, bool inflate);             // stores only
    bool connect(const char *host, int port);
    bool adopt(SOCKET accepted);                            // server side
    int  send(type, keys, vals, nkv, body, len, int waitTicks, bool compressible);
    bool start();                   // sender THEN reader
    bool drain(DWORD ms);           // phase 2
    void signalStop();              // phase 3: running=0, wake, shutdown(SD_SEND)
    bool join(DWORD eachMs);        // reader then sender
    bool release(bool externalSendersGone);  // gated frees; false => LEAK the object
};
```

## Commit sequence

- **a.** `wireFrameDelete`; `rc_frame_compressible(type)` in `frameproto.cpp` replacing the hard-coded EXECOUT/SYNCDAT/SYNCENT list; `rc_net_rx_bytes`/`tx_bytes` accessors (rpc.cpp and netcheck stop reading fields). No behaviour change.
- **b.** The codec statics gather into one per-connection state object, still in net.cpp. Committed-at-load must be byte-identical.
- **c.** The generic core moves to `wire/connection.{h,cpp}`; net.cpp becomes the adapter. `rc_net_send` with no connection returns DEAD (rpccheck's zeroed `rc_net`). Guest: drainprobe (24 busy acks before close), bridge restarted three times, `netcheck --slowsend`.
- **d.** The 66 KB static build buffer becomes per-connection heap (needed before two connections can exist). Measure.
- **e.** A reader-start failure tears down through the normal path (latent bug below). Own commit.
- **f.** `ConnectionService::join` reports the real answer; stale line numbers in BACKLOG 1 and mem-audit/03.
- **g.** `Listener` (bind/listen/accept, never SO_REUSEADDR), `Connection::adopt`, a reader idle timeout (select before recv, for the heartbeat), and a `wirecheck` exe that connects a Connection to its own Listener over loopback under Wine.

Each step ends with `make check`, `make test-fast`, `make wine-smoke` (netcheck/rpccheck run the real net code); b, c and e also on the guest.

## Invariants to preserve

1. Phase 1 (rpc worker drain + join) before `running=0`: the drain must still be able to send (net.cpp:1071).
2. FIN before join, `closesocket` after join; an abrupt RST is reported to crash Win98's Winsock (net.cpp:1129-1138).
3. No mutex on `sock`: ordering guards it; Win9x recycles descriptors (net.cpp:1118-1122).
4. Leak on join timeout, gates kept separate: socket needs rw&&sw; lock/events/outq need rw&&sw&&rpcj; inflate rw; deflate rpcj&&sw; WSACleanup rw&&sw (net.cpp:1169-1232).
5. Sticky `leaked` for the client (net.h:95-97). gremote cannot refuse forever: it needs frames tagged by connection/generation first (step 4).
6. `started=1` before the first failure return (net.cpp:522-531): once connect() created anything, release() must run.
7. The reader signals death: `running=0` on exit (net.cpp:381-389).
8. Latent bugs: a reader-start failure closesockets before joining the sender and discards its join result (net.cpp:748-761); a failed WSAStartup leaves an unbalanced WSACleanup (534-538); CONNECTED is posted after the reader starts (764), so FAILED can precede it.
9. The UI budget stays caller-chosen (5 ticks vs 50; net.h:131-135).
10. Backpressure before deflate, lock held through enqueue; hard cap (net.cpp:895-900, net.h:56-68).
11. Inflight order: rc_frame_free, HeapFree, then decrement; the sink frees on a failed post.
12. `_beginthreadex` with a non-NULL tid; start order rpc worker, sender, reader.
13. The reader never blocks on a window API; IMMEDIATE handlers never block or re-enter.

## Not moving in step 2

The RPC worker, exec and syncrpc (step 3, BACKLOG 9); the lane dispatcher (step 4, when gremote gives it a second user); HELLO/sysinfo (shared only when gremote's HELLO needs it); CANCEL (BACKLOG 6) and generations.

## Progress (2026-09-25)

- **a** `51ab54b`, **b** `74318c5`, **e** `8dc9670` (pulled ahead of c), **c** `47c7bb0`. Done and verified on the guest (RCC13/RCC14): three bridge restarts, a host change, an exec turn, drainprobe's 24 busy acks at close with no GPF. `netcheck --slowsend` runs in wine-smoke.
- Found on the way, both fixed:
  - Watcom's C++98 `new T()` does not zero a non-POD (CLAUDE.md).
  - A pre-existing one-byte heap overrun in the sync scanner (`bd4ecfc`, test `synccheck --scanner`), which crashed every build under a SYNCGET flood.
- drainprobe now arms the sync root and settles before the burst (`f55c5e1`, `a7014b4`). Run it on 7891 with the guest's port switched there if another bridge owns 7890; put it back afterwards.
- Left: **d** (build buffer per connection), **f** (ConnectionService::join, stale line numbers), **g** (Listener, adopt, idle timeout, wirecheck). None needs the guest until g's final check.
