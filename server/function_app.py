import azure.functions as func
import logging
import asyncio
import os
from io import BytesIO

# Set stateless mode via environment variable BEFORE importing FastMCP
# Azure functions are stateless and FastMCP normally maintains sessions.
# We ask it "not to track sessions" to better fit the serverless model.
os.environ["FASTMCP_STATELESS_HTTP"] = "true"

# Import MCP server
mcp = None           # The FastMCP server instance
mcp_asgi_app = None  # The ASGI web app (initialized lazily)
mcp_init_lock = None # Prevents multiple simultaneous initializations
lifespan_task = None # Background task keeping the app "alive"

try:
    from task_manager_streamable_http import mcp as mcp_instance
    mcp = mcp_instance
    logging.info("MCP server imported successfully")
except Exception as e:
    logging.error(f"Failed to import MCP server: {str(e)}", exc_info=True)
    mcp = None

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

async def initialize_mcp_app():
    """Initialize MCP ASGI app with proper lifespan handling."""
    # ASGI apps (like FastMCP) have a "lifespan" protocol to manage startup/running/shutdown.
    # normal ASGI servers handle this automatically, but in serverless we must do it manually.
    
    # Here's the flow:
    # 1. Create the ASGI app from FastMCP
    # 2. Create a lifespan scope and send lifespan.startup event
    # 3. Wait for lifespan.startup.complete event
    # 4. Keep the lifespan task running in the background to simulate a running server
    # 5. Use the ASGI app for handling requests
    # Note: We do not handle shutdown here, as serverless functions are ephemeral.
    
    # We call raw_app(lifespan_scope, receive, send)
    # App calls receive() → we return {"type": "lifespan.startup"}
    # App initializes its SessionManager
    # App calls send({"type": "lifespan.startup.complete"})
    # We set startup_complete event → initialization done!
    
    global mcp_asgi_app, lifespan_task
    
    # Get the ASGI app from FastMCP
    raw_app = mcp.http_app()
    
    # Create lifespan scope
    lifespan_scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
    
    # Track startup state
    startup_complete = asyncio.Event()
    startup_failed = False
    startup_error = None
    received_startup = False
    
    async def lifespan_receive():
        nonlocal received_startup
        if not received_startup:
            received_startup = True
            return {"type": "lifespan.startup"}     # Tell app to start
        await asyncio.Event().wait()                # then wait forever (don't send shutdown)
    
    async def lifespan_send(message):
        nonlocal startup_failed, startup_error
        msg_type = message.get("type", "")
        logging.info(f"Lifespan message: {msg_type}")
        
        if msg_type == "lifespan.startup.complete":
            startup_complete.set()                  # Signal that startup is done - App is ready!
        elif msg_type == "lifespan.startup.failed":
            startup_failed = True
            startup_error = message.get("message", "Unknown error")
            startup_complete.set()
    
    # Run lifespan in background task
    async def run_lifespan():
        try:
            await raw_app(lifespan_scope, lifespan_receive, lifespan_send)
        except Exception as e:
            logging.error(f"Lifespan task error: {e}")
    
    lifespan_task = asyncio.create_task(run_lifespan())         # Run in background
    
    
    try:
        await asyncio.wait_for(startup_complete.wait(), timeout=10.0)   # Wait for startup to complete (with timeout)
    except asyncio.TimeoutError:
        raise Exception("MCP lifespan startup timed out")
    
    if startup_failed:
        raise Exception(f"MCP lifespan startup failed: {startup_error}")
    
    mcp_asgi_app = raw_app
    logging.info("MCP ASGI app initialized with lifespan")
    return raw_app

@app.route(route="mcp", methods=["GET", "POST", "OPTIONS"])     # create the /api/mcp endpoint
async def mcp_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    """
    Azure Function HTTP trigger that handles MCP server requests.
    """
    logging.info(f'Processing MCP request: {req.method} {req.url}')
    
    # Handle CORS preflight requests
    if req.method == "OPTIONS":
        return func.HttpResponse(
            status_code=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization"
            }
        )
    
    if mcp is None:
        return func.HttpResponse(
            "MCP server not initialized",
            status_code=500,
            headers={"Access-Control-Allow-Origin": "*"}
        )
    
    # Lazy initialization of ASGI app on first request
    global mcp_asgi_app, mcp_init_lock
    if mcp_asgi_app is None:
        if mcp_init_lock is None:
            mcp_init_lock = asyncio.Lock()
        
        async with mcp_init_lock:
            if mcp_asgi_app is None:  # Double-check after acquiring lock
                logging.info("Initializing MCP ASGI app with lifespan...")
                await initialize_mcp_app()
                logging.info("MCP ASGI app ready")
    
    try:
        # Get the request body
        body = req.get_body()
        logging.info(f"Request body: {body[:200] if len(body) > 200 else body}")
        
        # Debug: Check what type req.headers is
        logging.info(f"Headers type: {type(req.headers)}")
        logging.info(f"Headers content: {req.headers}")
        
        # Create ASGI scope for FastMCP
        # Build proper headers list (list of 2-tuples of bytes)
        headers = []
        try:
            if hasattr(req.headers, 'items'):
                for key, value in req.headers.items():
                    logging.info(f"Header: {key} = {value} (types: {type(key)}, {type(value)})")
                    headers.append((str(key).lower().encode('latin-1'), str(value).encode('latin-1')))  # encode headers (keys and values) as latin-1 bytes
            else:
                logging.error(f"req.headers doesn't have items() method")
        except Exception as e:
            logging.error(f"Error building headers: {e}", exc_info=True)
        
        # Azure functions gives HttpRequest. ASGI apps expect a scope dictionary
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": req.method,
            "scheme": "http",
            "path": "/mcp",
            "query_string": req.url.split("?", 1)[1].encode('latin-1') if "?" in req.url else b"",
            "root_path": "",
            "headers": headers,
            "server": ("localhost", 7071),
        }
        
        # Response collection - variables to collect FastMCP response piece by piece
        # ASGI is a callback-based protocol. The app calls the two functions we provide below.
        
        response_started = False
        response_status = 200
        response_headers = []
        response_body = BytesIO()
        response_complete = asyncio.Event()
        receive_called = False
        
        async def receive():    
            # App asks for request data by calling this function
            nonlocal receive_called
            logging.info(f"RECEIVE called (first call: {not receive_called})")
            if not receive_called:
                receive_called = True
                # First call - return the request body
                return {
                    "type": "http.request",
                    "body": body,               # This is the JSON-RPC request
                    "more_body": False,         # No more data coming
                }
            else:
                # Subsequent calls - wait for disconnect or return immediately
                # This prevents blocking if FastMCP polls for more data
                # FastMCP uses SSE (Server-Sent Events) which keeps connections open. 
                # It polls receive() waiting for client disconnect. 
                # We send http.disconnect to signal "stop waiting".
                
                logging.info("RECEIVE called again - waiting briefly then signaling disconnect")
                await asyncio.sleep(0.1)
                return {"type": "http.disconnect"}
        
        async def send(message):
            # App sends response data by calling this function
            nonlocal response_started, response_status, response_headers
            logging.info(f"SEND called with message type: {message.get('type')}")
            
            if message["type"] == "http.response.start":
                response_started = True
                response_status = message["status"]
                response_headers = message.get("headers", [])
                logging.info(f"Response starting with status {response_status}, headers: {len(response_headers)}")
            elif message["type"] == "http.response.body":
                body_content = message.get("body", b"")
                if body_content:
                    logging.info(f"Received body chunk: {len(body_content)} bytes")
                    response_body.write(body_content)           # Collect the response body
                more_body = message.get("more_body", False)
                logging.info(f"More body coming: {more_body}")
                # Signal completion when no more body expected
                if not more_body:
                    response_complete.set()                     # Signal that response is complete  
        
        # Run ASGI app with timeout - SSE may keep connection open
        logging.info("Calling MCP ASGI app in stateless mode...")
        
        # FastMCP's SSE response never "ends" naturally - it's a streaming connection.
        # We:
        # 1) wait for response_complete (set when more_body=False)
        # 2) Cancel the task if it's still running
        # 3) Return whatever we collected
        
        async def run_asgi():
            await mcp_asgi_app(scope, receive, send)
        
        asgi_task = asyncio.create_task(run_asgi())
        
        try:
            # Wait for response to complete or timeout
            await asyncio.wait_for(response_complete.wait(), timeout=30.0)
            logging.info("Response complete signal received")
        except asyncio.TimeoutError:
            logging.warning("ASGI response timed out, returning collected data")
        
        # Cancel the ASGI task if still running
        if not asgi_task.done():
            asgi_task.cancel()
            try:
                await asgi_task
            except asyncio.CancelledError:
                pass
        
        logging.info("MCP ASGI app call completed")
        
        # Now convert collected ASGI response back into Azure Functions HttpResponse
        
        #         ┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
        #         │   curl/client   │────>│  Azure Functions │────>│    FastMCP      │
        #         │                 │     │   (HTTP Bridge)  │     │   (ASGI App)    │
        #         └─────────────────┘     └──────────────────┘     └─────────────────┘
        #                                          │
        #                               ┌──────────┴──────────┐
        #                               │                     │
        #                          HttpRequest            scope, receive, send
        #                               │                     │
        #                               ▼                     ▼
        #                          Azure Format          ASGI Protocol
        
        # Build response headers
        headers_dict = {"Access-Control-Allow-Origin": "*"}
        for header_name, header_value in response_headers:
            decoded_name = header_name.decode()
            decoded_value = header_value.decode()
            headers_dict[decoded_name] = decoded_value
            logging.info(f"Response header: {decoded_name} = {decoded_value}")
        
        response_content = response_body.getvalue()
        logging.info(f"Response status: {response_status}, body length: {len(response_content)}")
        logging.info(f"Response body preview: {response_content[:500]}")
        
        return func.HttpResponse(
            body=response_content,
            status_code=response_status,
            headers=headers_dict
        )
        
    except Exception as e:
        logging.error(f"Error processing request: {str(e)}", exc_info=True)
        return func.HttpResponse(
            f"Error: {str(e)}",
            status_code=500,
            headers={"Access-Control-Allow-Origin": "*"}
        )
