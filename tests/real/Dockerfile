# Real MCP tool tests — Ubuntu container with tmux/vim/less + aimux editable.
#
# Build:  docker build -t aimux-real-tests -f tests/real/Dockerfile .
# Run:    docker run --rm -v "$PWD":/opt/aimux aimux-real-tests
#
# The aimux source is mounted at /opt/aimux so editable-install picks up live
# edits; the bridge broker + reverse-connect + MCP server all run *inside*
# the container against real tmux PTYs on this container's kernel.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        tmux less file ca-certificates git curl \
        vim \
    && rm -rf /var/lib/apt/lists/*

# Python 3.10 (ubuntu:22.04 default) satisfies aimux's >=3.10 requirement.
WORKDIR /opt/aimux

# COPY-only image: the host's buildkit/runc intermittently fails RUN steps
# ("can't get final child's PID from pipe: EOF") while `docker run` itself
# is healthy, so ALL python setup happens in the entrypoint instead.
# The source is volume-mounted over /opt/aimux at run time anyway; these
# COPYs only make the image self-contained when run without the mount.
COPY pyproject.toml README.md /opt/aimux/
COPY src /opt/aimux/src
COPY tests/real/run.sh /opt/aimux/tests/real/run.sh


# bash -x: the bind mount shadows this COPY with the host file, which may
# not carry the +x bit — never rely on the exec bit.
ENTRYPOINT ["bash", "/opt/aimux/tests/real/run.sh"]
CMD ["tests/real"]
