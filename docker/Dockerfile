# The Gaston Code bridge, as a container.
#
# WHAT THIS IS FOR. The bridge is the modern half: it runs Claude Code and
# speaks the wire protocol to a Windows 98 machine over one TCP connection.
# It is the half that wants a current Python, a package index and a working
# network, which is exactly the half a container is good at. The guest side
# is two Win98 executables in a zip (tools/gen/mkrelease.sh) and has nothing
# to do with this image.
#
# CLAUDE CODE IS ALREADY IN HERE, AND NOT BECAUSE THIS FILE PUTS IT THERE.
# claude-agent-sdk ships one wheel per platform -- manylinux_2_17_x86_64 is
# 95 MB of the 98 -- and each one carries the CLI as package data at
# claude_agent_sdk/_bundled/claude, 228 MB unpacked. The SDK's
# `_find_bundled_cli()` is checked BEFORE shutil.which, so that copy is the
# one it actually runs.
#
# An earlier version of this file installed the CLI from Anthropic's native
# installer in a builder stage, on the stated premise that "the SDK does not
# contain Claude Code, it spawns it off PATH". That premise was wrong, and
# the image carried TWO copies of a 228 MB binary -- 773 MB, of which 279 MB
# was a CLI nothing ever executed. Asked directly, the SDK in that image
# answered `/usr/local/.../claude_agent_sdk/_bundled/claude`, not the one the
# Dockerfile had gone to the trouble of installing.
#
# So: no node, no npm, no installer, no builder stage. `pip install` is the
# whole of it.
#
# YOU STILL HAVE TO BRING CREDENTIALS. Nothing in this image can talk to
# Anthropic by itself -- pass ANTHROPIC_API_KEY, or bind-mount an
# authenticated ~/.claude and ~/.claude.json.
#
# Pinned base, pinned requirements. A release that resolves "whatever the
# index served that morning" is not a release.
FROM python:3.13.17-slim-trixie

# git because the agent is usually pointed at a repository. Nothing else:
# no compiler, no node.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements before source, so an edit to the bridge does not re-resolve
# and re-download the SDK's 95 MB wheel.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
 && python -c "\
from claude_agent_sdk._internal.transport.subprocess_cli import \
     SubprocessCLITransport as T; \
p = T._find_bundled_cli(None); \
print('bundled CLI:', p); \
assert p, 'the SDK wheel for this platform carries no bundled CLI'"

# bridge/ is the product. gremote/ comes too: it is the host-side driver for
# the guest server, it imports bridge.wire and bridge.agent, and somebody
# running the bridge in a container is exactly who wants to drive the guest
# from the same place.
COPY bridge/  /app/bridge/
COPY gremote/ /app/gremote/
COPY LICENSE NOTICE /app/

# The tree the agent works in and mirrors to the guest. A volume, because
# the whole point is that the work survives the container.
VOLUME ["/work"]

# The sync BASELINE -- what the bridge last believes the guest holds. It has
# to persist and it has to live outside /work: `--sync-state` inside the
# mirrored tree is mirrored to the guest and recurses, which is why
# bridge/server.py's own help says MUST. Without this it defaulted to
# /.gaston-state in the container's root filesystem, so every restart threw
# the baseline away and re-sent the whole tree over a 10 Mbit link.
VOLUME ["/state"]

# Where the Claude Code CLI keeps its credentials and session state. Mount
# an authenticated one from the host, or set ANTHROPIC_API_KEY instead.
# ~/.claude.json is a FILE and so cannot be declared here; bind-mount it
# alongside if you are using an OAuth login rather than a key.
VOLUME ["/root/.claude"]

EXPOSE 7890

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GASTON_PROG=gaston-bridge

# 0.0.0.0 INSIDE a container is not the same decision as 0.0.0.0 on a host.
# bridge/server.py's --host help says "use the retro-segment interface, not
# 0.0.0.0", and it is right about a host: that port grants a guest the
# ability to answer as the person at the keyboard. Here 0.0.0.0 covers the
# container's own interface only, and what reaches it is decided by how the
# port is published -- `-p 192.168.1.2:7890:7890` rather than `-p 7890:7890`.
# Same decision, made one layer out.
#
# GASTON_SECRET is NOT defaulted. bridge/server.py falls back to
# "dev-secret" with a warning when it is unset, which is right for a
# developer at a terminal and wrong for an image somebody pulled: pass
# -e GASTON_SECRET.
ENTRYPOINT ["python", "-m", "bridge.server"]
CMD ["--host", "0.0.0.0", "--port", "7890", \
     "--cwd", "/work", "--sync-dir", "/work", "--sync-state", "/state"]
