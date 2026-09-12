"""Agent-S3 action surface (verbatim signatures/docstrings).

Portions adapted from simular-ai/Agent-S (https://github.com/simular-ai/Agent-S,
commit 3aa272d, Apache License 2.0). See THIRD_PARTY_NOTICES.md at the
repository root. The surrounding OpenCuria project remains AGPL-3.0-only.
"""

# ruff: noqa: E501, UP006, UP035, UP045, I001

from typing import Any, Dict, List, Optional

def agent_action(func):
    """Mark a method as an AI-callable Agent-S action (mirrors Agent-S)."""
    func.is_agent_action = True
    return func


class AgentActionSurface:
    """Declares the exact Agent-S3 action interface the planner may call.

    Method signatures and docstrings are byte-identical to
    ``OSWorldACI`` in Agent-S (commit 3aa272d) because the worker prompt is
    built from them via ``dir()`` introspection. Bodies are never executed;
    the safe AST parser (:mod:`apps.harness.agent_s.parser`) plus the
    injected action materializer implement the behaviour without
    ``eval``/``exec``.
    """

    @agent_action
    def click(
        self,
        element_description: str,
        num_clicks: int = 1,
        button_type: str = "left",
        hold_keys: List = [],
    ):

        """Click on the element
        Args:
            element_description:str, a detailed descriptions of which element to click on. This description should be at least a full sentence.
            num_clicks:int, number of times to click the element
            button_type:str, which mouse button to press can be "left", "middle", or "right"
            hold_keys:List, list of keys to hold while clicking
        """

        raise NotImplementedError(
            "AgentActionSurface declares the interface only; "
            "use the parser + ActionMaterializer instead."
        )

    @agent_action
    def switch_applications(self, app_code):

        """Switch to a different application that is already open
        Args:
            app_code:str the code name of the application to switch to from the provided list of open applications
        """

        raise NotImplementedError(
            "AgentActionSurface declares the interface only; "
            "use the parser + ActionMaterializer instead."
        )

    @agent_action
    def open(self, app_or_filename: str):

        """Open any application or file with name app_or_filename. Use this action to open applications or files on the desktop, do not open manually.
        Args:
            app_or_filename:str, the name of the application or filename to open
        """

        raise NotImplementedError(
            "AgentActionSurface declares the interface only; "
            "use the parser + ActionMaterializer instead."
        )

    @agent_action
    def type(
        self,
        element_description: Optional[str] = None,
        text: str = "",
        overwrite: bool = False,
        enter: bool = False,
    ):

        """Type text/unicode into a specific element
        Args:
            element_description:str, a detailed description of which element to enter text in. This description should be at least a full sentence.
            text:str, the text to type
            overwrite:bool, Assign it to True if the text should overwrite the existing text, otherwise assign it to False. Using this argument clears all text in an element.
            enter:bool, Assign it to True if the enter key should be pressed after typing the text, otherwise assign it to False.
        """

        raise NotImplementedError(
            "AgentActionSurface declares the interface only; "
            "use the parser + ActionMaterializer instead."
        )

    @agent_action
    def save_to_knowledge(self, text: List[str]):

        """Save facts, elements, texts, etc. to a long-term knowledge bank for reuse during this task. Can be used for copy-pasting text, saving elements, etc.
        Args:
            text:List[str] the text to save to the knowledge
        """

        raise NotImplementedError(
            "AgentActionSurface declares the interface only; "
            "use the parser + ActionMaterializer instead."
        )

    @agent_action
    def drag_and_drop(
        self, starting_description: str, ending_description: str, hold_keys: List = []
    ):

        """Drag from the starting description to the ending description
        Args:
            starting_description:str, a very detailed description of where to start the drag action. This description should be at least a full sentence.
            ending_description:str, a very detailed description of where to end the drag action. This description should be at least a full sentence.
            hold_keys:List list of keys to hold while dragging
        """

        raise NotImplementedError(
            "AgentActionSurface declares the interface only; "
            "use the parser + ActionMaterializer instead."
        )

    @agent_action
    def highlight_text_span(
        self, starting_phrase: str, ending_phrase: str, button: str = "left"
    ):

        """Highlight a text span between a provided starting phrase and ending phrase. Use this to highlight words, lines, and paragraphs.
        Args:
            starting_phrase:str, the phrase that denotes the start of the text span you want to highlight. If you only want to highlight one word, just pass in that single word.
            ending_phrase:str, the phrase that denotes the end of the text span you want to highlight. If you only want to highlight one word, just pass in that single word.
            button:str, the button to use to highlight the text span. Defaults to "left". Can be "left", "right", or "middle".
        """

        raise NotImplementedError(
            "AgentActionSurface declares the interface only; "
            "use the parser + ActionMaterializer instead."
        )

    @agent_action
    def set_cell_values(
        self, cell_values: Dict[str, Any], app_name: str, sheet_name: str
    ):

        """Use this to set individual cell values in a spreadsheet. For example, setting A2 to "hello" would be done by passing {"A2": "hello"} as cell_values. The sheet must be opened before this command can be used.
        Args:
            cell_values: Dict[str, Any], A dictionary of cell values to set in the spreadsheet. The keys are the cell coordinates in the format "A1", "B2", etc.
                Supported value types include: float, int, string, bool, formulas.
            app_name: str, The name of the spreadsheet application. For example, "Some_sheet.xlsx".
            sheet_name: str, The name of the sheet in the spreadsheet. For example, "Sheet1".
        """

        raise NotImplementedError(
            "AgentActionSurface declares the interface only; "
            "use the parser + ActionMaterializer instead."
        )

    @agent_action
    def call_code_agent(self, task: str = None):

        """Call the code agent to execute code for tasks or subtasks that can be completed solely with coding.

        Args:
            task: str, the task or subtask to execute. If None, uses the current full task instruction.

        **🚨 CRITICAL GUIDELINES:**
        - **ONLY pass a task parameter for SPECIFIC subtasks** (e.g., "Calculate sum of column B", "Filter data by date")
        - **NEVER pass a task parameter for full tasks** - let it default to the original task instruction
        - **NEVER rephrase or modify the original task** - this prevents hallucination corruption
        - **If unsure, omit the task parameter entirely** to use the original task instruction

        Use this for tasks that can be fully accomplished through code execution, particularly for:
        - Spreadsheet applications (LibreOffice Calc, Excel): data processing, filtering, sorting, calculations, formulas, data analysis
        - Document editors (LibreOffice Writer, Word): text processing, content editing, formatting, document manipulation
        - Code editors (VS Code, text editors): code editing, file processing, text manipulation, configuration
        - Data analysis tools: statistical analysis, data transformation, reporting
        - File management: bulk operations, file processing, content extraction
        - System utilities: configuration, setup, automation
        """

        raise NotImplementedError(
            "AgentActionSurface declares the interface only; "
            "use the parser + ActionMaterializer instead."
        )

    @agent_action
    def scroll(self, element_description: str, clicks: int, shift: bool = False):

        """Scroll the element in the specified direction
        Args:
            element_description:str, a very detailed description of which element to enter scroll in. This description should be at least a full sentence.
            clicks:int, the number of clicks to scroll can be positive (up) or negative (down).
            shift:bool, whether to use shift+scroll for horizontal scrolling
        """

        raise NotImplementedError(
            "AgentActionSurface declares the interface only; "
            "use the parser + ActionMaterializer instead."
        )

    @agent_action
    def hotkey(self, keys: List):

        """Press a hotkey combination
        Args:
            keys:List the keys to press in combination in a list format (e.g. ['ctrl', 'c'])
        """

        raise NotImplementedError(
            "AgentActionSurface declares the interface only; "
            "use the parser + ActionMaterializer instead."
        )

    @agent_action
    def hold_and_press(self, hold_keys: List, press_keys: List):

        """Hold a list of keys and press a list of keys
        Args:
            hold_keys:List, list of keys to hold
            press_keys:List, list of keys to press in a sequence
        """

        raise NotImplementedError(
            "AgentActionSurface declares the interface only; "
            "use the parser + ActionMaterializer instead."
        )

    @agent_action
    def wait(self, time: float):

        """Wait for a specified amount of time
        Args:
            time:float the amount of time to wait in seconds
        """

        raise NotImplementedError(
            "AgentActionSurface declares the interface only; "
            "use the parser + ActionMaterializer instead."
        )

    @agent_action
    def done(
        self,
    ):

        """End the current task with a success. Use this when you believe the entire task has been fully completed."""

        raise NotImplementedError(
            "AgentActionSurface declares the interface only; "
            "use the parser + ActionMaterializer instead."
        )

    @agent_action
    def fail(self):

        """End the current task with a failure. Use this when you believe the entire task is impossible to complete."""

        raise NotImplementedError(
            "AgentActionSurface declares the interface only; "
            "use the parser + ActionMaterializer instead."
        )
