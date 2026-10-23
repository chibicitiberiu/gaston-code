# The Gaston Code bridge, as a container.
#
# WHAT THIS IS FOR. The bridge is the modern half: it runs Claude Code and
# speaks the wire protocol to a Windows 98 machine over one TCP connection.
# It is the half that wants a current Python, a package index and a working
# network, which is exactly the half a container is good at. The guest side
# is two Win98 executables in a zip (tools/gen/mkrelease.sh) and has nothing
# to do with this image.
#
# WHY CLAUDE CODE IS INSTALLED IN HERE. claude-agent-sdk does not contain
# it. The SDK SPAWNS the `claude` CLI, located through shutil.which and a
# short list of well-known paths, and raises an explanatory error when it
# finds neither (claude_agent_sdk/_internal/transport/subprocess_cli.py). So
# a bridge image without that CLI starts, accepts the guest's connection,
# and fails on the first prompt.
#
# THE NATIVE INSTALLER, NOT NPM, and it is worth 493 MB. Both were built
# and measured: `npm install -g @anthropic-ai/claude-code` needs nodejs and
# npm from apt and lands at **1,266 MB**; the native installer is a
# standalone binary and needs neither, at **773 MB**. Same CLI, same pinned
# version. Most of what is left is the CLI itself -- 279 MB of
# /root/.local -- so there is not much more to win here.
#
# YOU STILL HAVE TO BRING CREDENTIALS. Nothing in this image can talk to
# Anthropic by itself -- pass ANTHROPIC_API_KEY, or bind-mount an
# authenticated ~/.claude and ~/.claude.json.
#
# Pinned base, pinned CLI, pinned requirements. A release that resolves
# "whatever the index served that morning" is not a release.

# --------------------------------------------------------------- builder ---
# Here only so that curl, apt's lists and the installer's own ~250 MB of
# staging cache do not reach the image that gets pushed.
FROM python:3.13.17-slim-trixie AS claude

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# The installer takes `stable`, `latest` or an exact version. An exact one,
# deliberately: the CLI updates often, and an image that silently changes
# the agent's behaviour between two pulls of the same tag is an image nobody
# can debug. Raise it in a commit, so the change has a date.
ARG CLAUDE_CODE_VERSION=2.1.308
# pinned-dep-allow: install.sh is Anthropic's own installer for its own CLI,
# and the version it installs is pinned on the next line.
RUN curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_CODE_VERSION" \
 && /root/.local/bin/claude --version

# ----------------------------------------------------------------- image ---
FROM python:3.13.17-slim-trixie

# git because the agent is usually pointed at a repository. Nothing else:
# no compiler, no node.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# /root/.local holds the CLI launcher (bin/claude) and the versioned binary
# it resolves (share/claude). NOT /root/.claude, which the installer also
# creates and which is mounted over below -- the CLI and its credentials are
# deliberately in different trees so a mounted credential directory cannot
# shadow the program.
COPY --from=claude /root/.local /root/.local
ENV PATH="/root/.local/bin:${PATH}"
RUN claude --version

WORKDIR /app

# Requirements before source, so an edit to the bridge does not re-resolve
# and re-download thirty wheels.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

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
    PYTHONDONTWRITEBYTECODE=1

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
