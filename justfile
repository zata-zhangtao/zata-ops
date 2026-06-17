# ───────────────────────────────────────────────────────────────────────────────
# justfile — project-private recipes.
#
# Shared recipes live in `justfile.shared` and are kept in sync with the
# upstream template repository via `just sync-template`. Add or override
# project-specific commands below; same-name recipes here override shared ones.
# ───────────────────────────────────────────────────────────────────────────────

import 'justfile.shared'

# Run the development entrypoint
# Usage:
#   just run                 # start backend + frontend
#   just run backend         # start backend only
#   just run frontend        # start frontend only
#   just run docker          # start with Docker Compose (one-click deploy)
#   just run backend_port=8010 frontend_port=5178
#   just run all frontend_dir=web frontend_cmd="pnpm dev"
run arg1="" arg2="" arg3="" arg4="" arg5="" arg6="": _check-completion
    #!/usr/bin/env bash
    set -euo pipefail

    target="all"
    frontend_dir="frontend"
    backend_port=""
    frontend_port=""
    backend_cmd="uv run python -m backend.main"
    frontend_cmd="npm run dev"
    backend_pid=""
    frontend_pid=""
    run_state_file="$(git rev-parse --git-path vanta-run.env)"
    positional_index=0

    parse_run_arg() {
        cli_arg="$1"
        if [ -z "$cli_arg" ]; then
            return 0
        fi

        case "$cli_arg" in
            target=*)
                target="${cli_arg#target=}"
                ;;
            frontend_dir=*)
                frontend_dir="${cli_arg#frontend_dir=}"
                ;;
            backend_port=*)
                backend_port="${cli_arg#backend_port=}"
                ;;
            frontend_port=*)
                frontend_port="${cli_arg#frontend_port=}"
                ;;
            backend_cmd=*)
                backend_cmd="${cli_arg#backend_cmd=}"
                ;;
            frontend_cmd=*)
                frontend_cmd="${cli_arg#frontend_cmd=}"
                ;;
            *)
                case "$positional_index" in
                    0)
                        target="$cli_arg"
                        ;;
                    1)
                        frontend_dir="$cli_arg"
                        ;;
                    2)
                        backend_cmd="$cli_arg"
                        ;;
                    3)
                        frontend_cmd="$cli_arg"
                        ;;
                    *)
                        echo "ERROR: Unexpected run argument: $cli_arg"
                        echo "Usage: just run [backend|frontend|all|docker] [backend_port=<port>] [frontend_port=<port>]"
                        exit 1
                        ;;
                esac
                positional_index=$((positional_index + 1))
                ;;
        esac
    }

    for cli_arg in {{quote(arg1)}} {{quote(arg2)}} {{quote(arg3)}} {{quote(arg4)}} {{quote(arg5)}} {{quote(arg6)}}; do
        parse_run_arg "$cli_arg"
    done

    load_run_ports() {
        if [ -f "$run_state_file" ]; then
            # shellcheck disable=SC1090
            source "$run_state_file"
        fi

        backend_port="${backend_port:-${BACKEND_PORT:-8000}}"
        frontend_port="${frontend_port:-${FRONTEND_PORT:-5173}}"
    }

    save_run_ports() {
        mkdir -p "$(dirname "$run_state_file")"
        {
            printf 'BACKEND_PORT=%s\n' "$backend_port"
            printf 'FRONTEND_PORT=%s\n' "$frontend_port"
        } > "$run_state_file"
    }

    run_backend() {
        echo "Starting backend on port $backend_port: $backend_cmd"
        PORT="$backend_port" bash -lc "$backend_cmd"
    }

    run_frontend() {
        if [ ! -d "$frontend_dir" ]; then
            echo "ERROR: Frontend directory not found: $frontend_dir"
            echo "   Override it with: just run frontend frontend_dir=<path>"
            exit 1
        fi

        if [ ! -f "$frontend_dir/package.json" ]; then
            echo "ERROR: package.json not found in frontend directory: $frontend_dir"
            echo "   Override the directory or command, for example:"
            echo "   just run frontend frontend_dir=<path> frontend_cmd='pnpm dev'"
            exit 1
        fi

        echo "Starting frontend in $frontend_dir on port $frontend_port: $frontend_cmd"
        (
            cd "$frontend_dir"
            BACKEND_PORT="$backend_port" FRONTEND_PORT="$frontend_port" bash -lc "$frontend_cmd"
        )
    }

    cleanup_processes() {
        for process_pid in "$backend_pid" "$frontend_pid"; do
            if [ -n "$process_pid" ] && kill -0 "$process_pid" 2>/dev/null; then
                kill "$process_pid" 2>/dev/null || true
            fi
        done
        wait 2>/dev/null || true
    }

    wait_for_first_exit() {
        while true; do
            if [ -n "$backend_pid" ] && ! kill -0 "$backend_pid" 2>/dev/null; then
                wait "$backend_pid"
                return $?
            fi

            if [ -n "$frontend_pid" ] && ! kill -0 "$frontend_pid" 2>/dev/null; then
                wait "$frontend_pid"
                return $?
            fi

            sleep 1
        done
    }

    load_run_ports
    save_run_ports
    echo "Saved run ports to $run_state_file"

    case "$target" in
        backend)
            run_backend
            ;;
        frontend)
            run_frontend
            ;;
        all)
            trap cleanup_processes EXIT INT TERM
            run_backend &
            backend_pid=$!
            run_frontend &
            frontend_pid=$!
            wait_for_first_exit
            ;;
        docker)
            echo "Starting services with Docker Compose..."
            if [ ! -f ".env.local" ]; then
                echo ".env.local is required for 'just run docker'. Copy .env.example to"
                echo ".env.local and set your own service addresses (DATABASE_URL, S3_*, ...)."
                exit 1
            fi
            # Containers cannot reach the host via localhost/127.0.0.1; only the
            # backend-facing DATABASE_URL / S3_ENDPOINT need host.docker.internal.
            # Generate .env.local.docker from .env.local on first run, then keep
            # the generated file so users can tweak it manually without being overwritten.
            compose_env_file=".env.local.docker"
            if [ -f "$compose_env_file" ]; then
                echo "Using existing $compose_env_file (delete it to regenerate from .env.local)"
            else
                sed -E \
                    -e '/^(DATABASE_URL|S3_ENDPOINT)=/ s#(@|//)(localhost|127\.0\.0\.1)#\1host.docker.internal#g' \
                    .env.local > "$compose_env_file"
                echo "Generated $compose_env_file from .env.local (localhost -> host.docker.internal for DATABASE_URL/S3_ENDPOINT)"
            fi
            # Layer env like settings.py: load .env first, then .env.local.docker overrides it.
            env_file_args=()
            [ -f ".env" ] && env_file_args+=(--env-file .env)
            env_file_args+=(--env-file "$compose_env_file")
            COMPOSE_LOCAL_ENV_FILE="$compose_env_file" docker compose "${env_file_args[@]}" up --build
            ;;
        *)
            echo "ERROR: Unknown run target: $target"
            echo "Usage: just run [backend|frontend|all|docker]"
            exit 1
            ;;
    esac

# Stop local development services by remembered or provided ports.
# Usage:
#   just down
#   just down backend
#   just down frontend
#   just down backend_port=8010 frontend_port=5178
#   just down docker
down arg1="" arg2="" arg3="": _check-completion
    #!/usr/bin/env bash
    set -euo pipefail

    target="all"
    backend_port=""
    frontend_port=""
    run_state_file="$(git rev-parse --git-path vanta-run.env)"
    positional_index=0

    parse_down_arg() {
        cli_arg="$1"
        if [ -z "$cli_arg" ]; then
            return 0
        fi

        case "$cli_arg" in
            target=*)
                target="${cli_arg#target=}"
                ;;
            backend_port=*)
                backend_port="${cli_arg#backend_port=}"
                ;;
            frontend_port=*)
                frontend_port="${cli_arg#frontend_port=}"
                ;;
            *)
                if [ "$positional_index" -eq 0 ]; then
                    target="$cli_arg"
                    positional_index=1
                else
                    echo "ERROR: Unexpected down argument: $cli_arg"
                    echo "Usage: just down [backend|frontend|all|docker] [backend_port=<port>] [frontend_port=<port>]"
                    exit 1
                fi
                ;;
        esac
    }

    for cli_arg in {{quote(arg1)}} {{quote(arg2)}} {{quote(arg3)}}; do
        parse_down_arg "$cli_arg"
    done

    load_run_ports() {
        if [ -f "$run_state_file" ]; then
            # shellcheck disable=SC1090
            source "$run_state_file"
        fi

        backend_port="${backend_port:-${BACKEND_PORT:-8000}}"
        frontend_port="${frontend_port:-${FRONTEND_PORT:-5173}}"
    }

    stop_port() {
        port_label="$1"
        port_value="$2"
        # Exclude Docker Desktop / dockerd processes so just down does not kill the Docker daemon
        process_ids="$(lsof -nP -iTCP:"$port_value" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 && $1 !~ /^(com\.docker|docker|vpnkit|hyperkit)/ {print $2}' | sort -u || true)"

        if [ -z "$process_ids" ]; then
            echo "No $port_label process listening on port $port_value"
            return 0
        fi

        echo "Stopping $port_label process(es) on port $port_value: $process_ids"
        kill $process_ids 2>/dev/null || true
        sleep 1

        remaining_process_ids="$(lsof -nP -iTCP:"$port_value" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 && $1 !~ /^(com\.docker|docker|vpnkit|hyperkit)/ {print $2}' | sort -u || true)"
        if [ -n "$remaining_process_ids" ]; then
            echo "Force stopping $port_label process(es) on port $port_value: $remaining_process_ids"
            kill -9 $remaining_process_ids 2>/dev/null || true
        fi
    }

    load_run_ports

    case "$target" in
        backend)
            stop_port backend "$backend_port"
            ;;
        frontend)
            stop_port frontend "$frontend_port"
            ;;
        all)
            stop_port backend "$backend_port"
            stop_port frontend "$frontend_port"
            ;;
        docker)
            docker compose down
            ;;
        *)
            echo "ERROR: Unknown down target: $target"
            echo "Usage: just down [backend|frontend|all|docker]"
            exit 1
            ;;
    esac


# ── Frontend ──────────────────────────────────────────────────────────────────

# Frontend helper
# Usage:
#   just frontend dev
#   just frontend build
#   just frontend install
frontend action="dev":
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}/frontend"
    case "{{action}}" in
        dev)
            npm run dev
            ;;
        build)
            npm run build
            ;;
        install)
            npm install
            ;;
        *)
            echo "ERROR: Unknown action: {{action}}"
            echo "Usage: just frontend [dev|build|install]"
            exit 1
            ;;
    esac


# ── Local Testing Middleware ──────────────────────────────────────────────────

# Manage the local testing middleware stack (docker-compose.testing.yml).
# Usage:
#   just testing                     # show running services (no side effects)
#   just testing up                  # apply compose changes / start all
#   just testing up ragflow          # apply / start a single service
#   just testing restart ragflow     # restart a service without rebuilding
#   just testing recreate ragflow    # force-recreate (picks up new image)
#   just testing recreate            # force-recreate all services
#   just testing down                # stop and remove the stack
testing action="ps" service="":
    #!/usr/bin/env bash
    set -euo pipefail
    cd {{justfile_directory()}}
    compose_file="docker-compose.testing.yml"

    case "{{action}}" in
        ps)
            docker compose -f "$compose_file" ps
            ;;
        up)
            if [ -n "{{service}}" ]; then
                echo "Starting '{{service}}' from $compose_file"
                docker compose -f "$compose_file" up -d "{{service}}"
            else
                echo "Starting all services from $compose_file"
                docker compose -f "$compose_file" up -d
            fi
            ;;
        restart)
            if [ -n "{{service}}" ]; then
                echo "Restarting '{{service}}'"
                docker compose -f "$compose_file" restart "{{service}}"
            else
                echo "Restarting all services"
                docker compose -f "$compose_file" restart
            fi
            ;;
        recreate)
            if [ -n "{{service}}" ]; then
                echo "Force-recreating '{{service}}'"
                docker compose -f "$compose_file" up -d --force-recreate "{{service}}"
            else
                echo "Force-recreating all services"
                docker compose -f "$compose_file" up -d --force-recreate
            fi
            ;;
        down)
            echo "Stopping and removing the $compose_file stack"
            docker compose -f "$compose_file" down
            ;;
        *)
            echo "ERROR: Unknown testing action: {{action}}"
            echo "Usage: just testing [ps|up|restart|recreate|down] [service]"
            exit 1
            ;;
    esac
