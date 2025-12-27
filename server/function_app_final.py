import azure.functions as func
import logging
import asyncio
from io import BytesIO

# Import MCP server
try:
    from task_manager_streamable_http import mcp
    logging.info("MCP server imported successfully")
except Exception as e:
    logging.error(f"Failed to import MCP server: {str(e)}")
    mcp = None

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

@app.route(route="mcp", methods=["GET", "POST", "OPTIONS"])
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
    
    try:
        # Get the request body
        body = req.get_body()
        logging.info(f"Request body: {body[:200] if len(body) > 200 else body}")
        
        # Create ASGI scope for FastMCP
        # ASGI is the standard Python async web server interface
        # FastMCP is an ASGI application
        # This dictionary tells FastMCP about the incoming request
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": req.method,
            "scheme": "https",
            "path": "/mcp",
            "query_string": req.url.split("?", 1)[1].encode() if "?" in req.url else b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in req.headers.items()],
            "server": ("func-mcp-tasks-pdo1.azurewebsites.net", 443),
        }
        
        # Response collection - variables to collect FastMCP response piece by piece
        response_started = False
        response_status = 200
        response_headers = []
        response_body = BytesIO()
        
        async def receive():
            # This function provides the request body to FastMCP
            # Since we have the full body, we send it all at once
            return {
                "type": "http.request",
                "body": body,
                "more_body": False,
            }
        
        async def send(message):
            # This function collects the response from FastMCP
            # It handles both the response start and body messages
            # FastMCP calls this to send response data
            # We collect the status, headers, and body chunks
            # Notice that ASGI sends responses in pieces: first "start" (status + headers), then "body"
            nonlocal response_started, response_status, response_headers
            
            if message["type"] == "http.response.start":
                response_started = True
                response_status = message["status"]
                response_headers = message.get("headers", [])
            elif message["type"] == "http.response.body":
                body_content = message.get("body", b"")
                if body_content:
                    response_body.write(body_content)
        
        # Call FastMCP's ASGI application
        # Passes the scope (request info), receive (to read body), and send (to write response)
        # FastMCP processes the MCP protocol request and calls our send() with the response
        await mcp._mcp_app(scope, receive, send)
        
        # Build response headers
        # Convert FastMCP's response back to Azure Functions format
        # Converts headers from ASGI format (bytes tuples) to Azure format (dict)
        # Gets the complete body from BytesIO
        # Returns Azure HttpResponse
        headers_dict = {"Access-Control-Allow-Origin": "*"}
        for header_name, header_value in response_headers:
            headers_dict[header_name.decode()] = header_value.decode()
        
        response_content = response_body.getvalue()
        logging.info(f"Response status: {response_status}, body length: {len(response_content)}")
        
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
