FROM debian:trixie-slim AS dev

ENV PYTHONUNBUFFERED=1
ENV TERM=xterm-256color
ENV COLORTERM=truecolor
ENV DEBIAN_FRONTEND=noninteractive
ENV UV_COMPILE_BYTECODE=1

RUN apt update --yes --quiet && apt install --yes --quiet --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    wget \
    jq \
    rsync \
    tmux \
    git \
    git-lfs \
    sudo \
    vim \
    zsh \
    locales \
    openssh-client \
    procps \
    gnupg \
    lsb-release \
    && rm -rf /var/lib/apt/lists/*

# Fix locale (resolves VSCode remote terminal issues)
RUN sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && \
    locale-gen

# Node.js 24
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node -v && npm -v

# Google Cloud SDK
RUN echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list \
    && curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && apt-get update && apt-get install -y --no-install-recommends google-cloud-cli \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI. Installed here rather than via the devcontainer feature so it exists under
# a plain `docker compose up` too — features are applied only by the devcontainer CLI,
# and `make init` talks to compose directly.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* \
    && gh --version

# uv — owns the Python toolchain too, so there is no system Python to disagree with it.
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/

ARG USERNAME=dev
ARG USER_UID=1000
ARG USER_GID=$USER_UID

RUN groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID -m -s /bin/zsh $USERNAME \
    && echo $USERNAME ALL=\(root\) NOPASSWD:ALL > /etc/sudoers.d/$USERNAME \
    && chmod 0440 /etc/sudoers.d/$USERNAME

RUN mkdir -p /app/.venv \
    && chown -R $USERNAME:$USERNAME /app

RUN mkdir -p /home/dev/.vscode-server \
    && mkdir -p /home/dev/history \
    && chown -R $USERNAME:$USERNAME /home/dev/

WORKDIR /app

RUN chown -R $USERNAME:$USERNAME /app
USER $USERNAME

# Every tool below installs under $HOME so it is user-owned and updatable in place
# without a rebuild. See the update commands in README.md.
ENV PATH="/app/.venv/bin:/home/dev/.local/bin:$PATH"

# Python, managed by uv rather than by the base image.
RUN uv python install 3.13

# Credential homes. These are named volumes at run time (see compose.yaml), and Docker
# seeds a named volume from the image *only while it is empty* — so the directories must
# exist here with the right ownership and modes, or ssh mounts them root-owned and
# refuses to read the keys. Editing the starter config below will NOT reach an existing
# volume; remove the volume to re-seed it.
RUN mkdir -p /home/dev/.ssh /home/dev/.config/gh \
    && chmod 700 /home/dev/.ssh \
    && ssh-keyscan github.com > /home/dev/.ssh/known_hosts 2>/dev/null \
    && chmod 600 /home/dev/.ssh/known_hosts \
    && printf '%s\n' \
    'Host github.com' \
    '  hostname github.com' \
    '  user git' \
    '  identitiesOnly yes' \
    '  identityFile ~/.ssh/id_github' \
    '  controlMaster no' \
    > /home/dev/.ssh/config \
    && chmod 600 /home/dev/.ssh/config

# Claude Code CLI — the harness is authored interactively in this container.
# Uses the official native installer (the npm `install` subcommand no longer
# self-bootstraps the platform binary under npx).
# codegraph is the pre-indexed code graph agents query over MCP instead of crawling files.
# @openai/codex is the Codex CLI.
# pi the coding cli
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && echo 'export PATH="$HOME/.local/bin:$PATH"' >> ${HOME}/.zshrc

# Node-based CLIs, installed user-local so `npm update -g` needs no sudo. codegraph is
# the pre-indexed code graph agents query over MCP instead of crawling files; see
# .mcp.json. `make init` runs `codegraph init` to build the index, and every builder
# re-runs it inside its worktree. Versions pinned so a rebuild is reproducible; bump
# deliberately.
ARG CODEGRAPH_VERSION=latest
ARG CODEX_VERSION=latest
ARG PI_VERSION=latest
RUN npm config set prefix "$HOME/.local" \
    && npm install -g \
        "@colbymchenry/codegraph@${CODEGRAPH_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
        "@earendil-works/pi-coding-agent@${PI_VERSION}"

# oh-my-zsh + plugins
RUN sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended \
    && git clone --depth=1 https://github.com/zsh-users/zsh-autosuggestions ~/.oh-my-zsh/custom/plugins/zsh-autosuggestions \
    && git clone --depth=1 https://github.com/zsh-users/zsh-syntax-highlighting ~/.oh-my-zsh/custom/plugins/zsh-syntax-highlighting

RUN sed -i 's/^plugins=.*/plugins=(git docker node npm zsh-autosuggestions zsh-syntax-highlighting)/' ~/.zshrc

RUN echo 'alias ll="ls -lah"' >> ~/.zshrc \
    && echo 'alias gemini="GOOGLE_CLOUD_PROJECT= npx @google/gemini-cli"' >> ~/.zshrc \
    && echo 'export HISTFILE=$HOME/history/.zsh_history' >> ~/.zshrc
