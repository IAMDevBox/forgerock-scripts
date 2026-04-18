#!/bin/bash
# Tomcat stop → replace war → start script
# Usage: tomcat-restart.sh <CATALINA_HOME> [war_file]

set -e

CATALINA_HOME="${1:?Usage: $0 <CATALINA_HOME> [war_file]}"
WAR_FILE="$2"
SHUTDOWN_TIMEOUT=30
WEBAPPS="$CATALINA_HOME/webapps"

# --- Stop ---
echo "Stopping Tomcat..."
"$CATALINA_HOME/bin/shutdown.sh" 2>/dev/null || true

# Wait for graceful shutdown
elapsed=0
while [ $elapsed -lt $SHUTDOWN_TIMEOUT ]; do
    pid=$(pgrep -f "catalina.base=$CATALINA_HOME" 2>/dev/null || true)
    [ -z "$pid" ] && break
    sleep 1
    elapsed=$((elapsed + 1))
    echo "  Waiting for shutdown... ($elapsed/${SHUTDOWN_TIMEOUT}s)"
done

# Force kill if still running
pid=$(pgrep -f "catalina.base=$CATALINA_HOME" 2>/dev/null || true)
if [ -n "$pid" ]; then
    echo "  Graceful shutdown failed, force killing PID $pid"
    kill -9 $pid
    sleep 2
fi

echo "Tomcat stopped."

# --- Replace war ---
if [ -n "$WAR_FILE" ]; then
    if [ ! -f "$WAR_FILE" ]; then
        echo "Error: war file not found: $WAR_FILE"
        exit 1
    fi
    APP_NAME=$(basename "$WAR_FILE" .war)
    echo "Removing old deployment: $APP_NAME"
    rm -rf "$WEBAPPS/$APP_NAME"
    rm -f "$WEBAPPS/$APP_NAME.war"
    echo "Deploying: $WAR_FILE"
    cp "$WAR_FILE" "$WEBAPPS/"
fi

# --- Start ---
echo "Starting Tomcat..."
"$CATALINA_HOME/bin/startup.sh"
echo "Done."
