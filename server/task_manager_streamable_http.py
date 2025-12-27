from fastmcp import FastMCP
import csv
from typing import Optional
from typing import Annotated
from datetime import datetime
from pathlib import Path
from enum import Enum
import os
import io

# Azure Blob Storage for persistent CSV
from azure.storage.blob import BlobServiceClient

# Create the MCP server
mcp = FastMCP("Tasks MCP Server")

class Tags(Enum):
    WORK = "work"
    HOME = "home"
    SPORT = "sport"

class Status(Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    IN_PROGRESS = "in-progress"

# In-memory storage
tasks: list[dict] = []

def get_next_task_id() -> int:  
    if not tasks:
        return 1
    return max(task["id"] for task in tasks) + 1


# Azure Blob Storage configuration
STORAGE_CONNECTION_STRING = os.getenv("AzureWebJobsStorage")
CONTAINER_NAME = "mcp-tasks"
BLOB_NAME = "tasks.csv"

# Initialize blob client
blob_service_client = None
blob_client = None

if STORAGE_CONNECTION_STRING:
    try:
        blob_service_client = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
        # Create container if it doesn't exist
        try:
            blob_service_client.create_container(CONTAINER_NAME)
        except Exception:
            pass  # Container already exists
        blob_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=BLOB_NAME)
    except Exception as e:
        print(f"Failed to initialize blob storage: {e}")

# Helper functions
def read_tasks() -> list[dict]:
    if blob_client:
        try:
            # Download from blob storage
            blob_data = blob_client.download_blob().readall()
            csv_content = blob_data.decode("utf-8")
            reader = csv.DictReader(io.StringIO(csv_content))
            return [dict(row, id=int(row["id"])) for row in reader]
        except Exception as e:
            # Blob doesn't exist or error - return empty list
            print(f"Error reading from blob: {e}")
            return []
    else:
        # Fallback to local file
        CSV_FILE = Path(os.getenv("TEMP", "/tmp")) / "tasks.csv"
        if not CSV_FILE.exists():
            return []
        with CSV_FILE.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return [dict(row, id=int(row["id"])) for row in reader]

def write_tasks(tasks: list[dict]):
    # Write to in-memory buffer
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["id","description","tag","status","created_at","due_date"])
    writer.writeheader()
    for task in tasks:
        writer.writerow({
            "id": task["id"],
            "description": task["description"],
            "tag": task.get("tag",""),
            "status": task["status"],
            "created_at": task["created_at"],
            "due_date": task.get("due_date","")
        })
    
    csv_content = output.getvalue()
    
    if blob_client:
        try:
            # Upload to blob storage
            blob_client.upload_blob(csv_content, overwrite=True)
        except Exception as e:
            print(f"Error writing to blob: {e}")
    else:
        # Fallback to local file
        CSV_FILE = Path(os.getenv("TEMP", "/tmp")) / "tasks.csv"
        with CSV_FILE.open("w", newline="", encoding="utf-8") as f:
            f.write(csv_content)

def get_next_task_id() -> int:
    tasks = read_tasks()
    if not tasks:
        return 1
    return max(task["id"] for task in tasks) + 1


# Tool to add a task
@mcp.tool
def add_task(
    description: str, 
    tag: Optional[str] = None, 
    due_date: Optional[str] = None
) -> dict:
    """
    Add a new task to the task manager and persist it to CSV.
    
    Creates a new task with a unique ID, sets its status to 'pending', and records
    the creation timestamp. The task is appended to the existing tasks list and
    saved to the CSV file.
    
    Args:
        description (str): The description of the task to be added.
        tag (Optional[str]): An optional tag to categorize the task (e.g., 'work', 'personal').
        due_date (Optional[str]): An optional due date for the task in ISO format or custom format.
    
    Returns:
        dict: A dictionary containing the newly created task with the following keys:
            - id (int): Unique identifier for the task
            - description (str): Task description
            - tag (str): Task tag (empty string if not provided)
            - status (str): Task status (always 'pending' for new tasks)
            - created_at (str): ISO format timestamp of task creation
            - due_date (str): Due date (empty string if not provided)
    """
    tasks = read_tasks()  # load current tasks from CSV
    task_id = get_next_task_id()
    task = {
        "id": task_id,
        "description": description,
        "tag": tag or "",
        "status": Status.PENDING.value,
        "created_at": datetime.now().isoformat(),
        "due_date": due_date or ""
    }
    tasks.append(task)
    write_tasks(tasks)  # save back to CSV
    return task

# Tool to list all tasks
@mcp.tool(name="list_tasks", description="List all tasks, optionally filtering by tag.")
def list_tasks(
        tag: Optional[str] = None
    ) -> list[dict]:
    """
    Retrieve all tasks from the task manager, with optional filtering by tag.
    
    Loads tasks from the CSV file and returns them as a list. If a tag is provided,
    only tasks matching that specific tag will be returned.
    
    Args:
        tag (Optional[Annotated[Tags, "Task tag"]]): An optional tag to filter tasks by. If provided, only tasks with this exact tag will be returned. If None, all tasks are returned.
    
    Returns:
        list[dict]: A list of task dictionaries, each containing:
            - id (int): Unique identifier for the task
            - description (str): Task description
            - tag (str): Task tag (may be empty string)
            - status (str): Current status of the task ('pending', 'completed', etc.)
            - created_at (str): ISO format timestamp of task creation
            - due_date (str): Due date (may be empty string)
    """
    tasks = read_tasks()
    if tag:
        tasks = [task for task in tasks if task.get("tag") == tag]
    return tasks

# Tool to update a task
@mcp.tool(name="update_task", description="Update one or more fields of an existing task by ID.")
def update_task(
    task_id: int,
    description: Optional[str] = None,
    tag: Optional[str] = None,
    due_date: Optional[str] = None,
    status: Optional[str] = None
) -> dict:
    """
    Update one or more fields of an existing task and persist changes to CSV.
    
    Searches for a task by its unique ID and updates any provided fields. At least
    one field should be provided to update. The updated task list is saved back to
    the CSV file.
    
    Args:
        task_id (int): The unique identifier of the task to update.
        description (Optional[str]): New description for the task. If None, keeps existing value.
        tag (Optional[Annotated[Tags, "Task tag"]]): New tag for the task. If None, keeps existing value.
        due_date (Optional[str]): New due date for the task. If None, keeps existing value.
        status (Optional[str]): New status for the task (e.g., 'pending', 'completed', 'in-progress').
            If None, keeps existing value.
    
    Returns:
        dict: The updated task dictionary with all current values:
            - id (int): Unique identifier for the task
            - description (str): Task description (updated or existing)
            - tag (str): Task tag (updated or existing)
            - status (str): Task status (updated or existing)
            - created_at (str): ISO format timestamp of task creation (unchanged)
            - due_date (str): Due date (updated or existing)
    
    Raises:
        ValueError: If no task with the specified task_id is found.
    """
    tasks = read_tasks()
    for task in tasks:
        if task["id"] == task_id:
            if description is not None:
                task["description"] = description
            if tag is not None:
                task["tag"] = tag
            if due_date is not None:
                task["due_date"] = due_date
            if status is not None:
                task["status"] = status
            write_tasks(tasks)
            return task
    raise ValueError(f"Task with ID {task_id} not found.")

# Tool to delete a task
@mcp.tool(name="delete_task", description="Delete a task by ID.")
def delete_task(task_id: int) -> dict:
    """
    Delete a task from the task manager by its unique ID and persist changes to CSV.
    
    Searches for a task by its unique ID, removes it from the task list, and saves
    the updated list back to the CSV file.
    
    Args:
        task_id (int): The unique identifier of the task to delete.
    
    Returns:
        dict: The deleted task dictionary containing:
            - id (int): Unique identifier for the task
            - description (str): Task description
            - tag (str): Task tag
            - status (str): Task status at time of deletion
            - created_at (str): ISO format timestamp of task creation
            - due_date (str): Due date
    
    Raises:
        ValueError: If no task with the specified task_id is found.
    """
    tasks = read_tasks()
    for i, task in enumerate(tasks):
        if task["id"] == task_id:
            deleted_task = tasks.pop(i)
            write_tasks(tasks)
            return deleted_task
    raise ValueError(f"Task with ID {task_id} not found.")

# Tool to get a task by ID
@mcp.tool(name="get_task", description="Get the details of a task by ID.")
def get_task(task_id: int) -> dict:
    """
    Retrieve a specific task by its unique ID from the task manager.
    
    Loads tasks from the CSV file and searches for a task matching the provided ID.
    This is useful for getting detailed information about a single task without
    retrieving the entire task list.
    
    Args:
        task_id (int): The unique identifier of the task to retrieve.
    
    Returns:
        dict: The task dictionary containing:
            - id (int): Unique identifier for the task
            - description (str): Task description
            - tag (str): Task tag (may be empty string)
            - status (str): Current status of the task ('pending', 'completed', etc.)
            - created_at (str): ISO format timestamp of task creation
            - due_date (str): Due date (may be empty string)
    
    Raises:
        ValueError: If no task with the specified task_id is found.
    """
    tasks = read_tasks()
    for task in tasks:
        if task["id"] == task_id:
            return task
    raise ValueError(f"Task with ID {task_id} not found.")


# Add a prompt 
@mcp.prompt
def analyze_tasks(
    tag: Optional[str] = None,
    status: Optional[str] = None
    ) -> str:
    """Generate a prompt to analyze the tasks with optional filters."""

    filters = []
    if tag:
        filters.append(f'tag="{tag}"')
        
    if status:
        filters.append(f'status="{status}"')
        
    filters_text =  f" ({', '.join(filters)})" if filters else ""
    
    return f"""
Please analyze the tasks{filters_text} and provide a summary report including:
- Total number of tasks
- Number of tasks by status
- Any overdue tasks (if due_date is set)
- Suggestions for prioritization or next steps

Provide the report in markdown format
"""

# Resource to get all tasks
@mcp.resource("tasks://all_tasks")
def tasks() -> list[dict]:
    """
    Resource: List of all tasks.
    Returns all tasks as a list of dictionaries.
    """
    return read_tasks()



if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
