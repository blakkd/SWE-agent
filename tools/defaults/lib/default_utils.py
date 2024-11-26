import json
import os
from pathlib import Path
from typing import Any, Optional, Tuple, Union


class EnvRegistry:
    """Read and write variables into a file. This is used to persist state between tool
    calls without using environment variables (which are problematic because you cannot
    set them in a subprocess).

    The default file location is `/root/.swe-agent-env`, though this can be overridden
    by the `env_file` argument or the `SWE_AGENT_ENV_FILE` environment variable.
    """

    def __init__(self, env_file: Optional[Path] = None):
        self._env_file = env_file

    @property
    def env_file(self) -> Path:
        if self._env_file is None:
            env_file = Path(os.environ.get("SWE_AGENT_ENV_FILE", "/root/.swe-agent-env"))
        else:
            env_file = self._env_file
        if not env_file.exists():
            env_file.write_text("{}")
        return env_file

    def __getitem__(self, key: str) -> str:
        return json.loads(self.env_file.read_text())[key]

    def get(self, key: str, default_value: Any = None) -> Any:
        return json.loads(self.env_file.read_text()).get(key, default_value)

    def get_if_none(self, value: Any, key: str, default_value: Any = None) -> Any:
        if value is not None:
            return value
        return self.get(key, default_value)

    def __setitem__(self, key: str, value: Any):
        env = json.loads(self.env_file.read_text())
        env[key] = value
        self.env_file.write_text(json.dumps(env))


registry = EnvRegistry()


class FileNotOpened(Exception):
    """Raised when no file is opened."""


class TextNotFound(Exception):
    """Raised when the text is not found in the window."""


def _find_all(a_str: str, sub: str):
    start = 0
    while True:
        start = a_str.find(sub, start)
        if start == -1:
            return
        yield start
        start += len(sub)


class ReplacementInfo:
    def __init__(self, first_replaced_line: int, n_search_lines: int, n_replace_lines: int, n_replacements: int):
        self.first_replaced_line = first_replaced_line
        self.n_search_lines = n_search_lines
        self.n_replace_lines = n_replace_lines
        self.n_replacements = n_replacements


class InsertInfo:
    def __init__(self, first_inserted_line: int, n_lines_added: int):
        self.first_inserted_line = first_inserted_line
        self.n_lines_added = n_lines_added


class WindowedFile:
    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        first_line: Optional[int] = None,
        window: Optional[int] = None,
        exit_on_exception: bool = True,
    ):
        """

        Args:
            path: Path to the file to open.
            first_line: First line of the display window.
            window: Number of lines to display.
            exit_on_exception: If False, will raise exception.
                If true, will print an error message and exit.

        Will create file if not found.

        Internal convention/notes:

        * All line numbers are 0-indexed.
        * Previously, we used "current_line" for the internal state
          of the window position, pointing to the middle of the window.
          Now, we use `first_line` for this purpose (it's simpler this way).
        """
        _path = registry.get_if_none(path, "CURRENT_FILE")
        self._exit_on_exception = exit_on_exception
        if not _path:
            if self._exit_on_exception:
                print("No file open. Use the open command first.")
                exit(1)
            raise FileNotOpened
        self.path = Path(_path)
        if self.path.is_dir():
            msg = f"Error: {self.path} is a directory. You can only open files. Use cd or ls to navigate directories."
            if self._exit_on_exception:
                print(msg)
                exit(1)
            raise IsADirectoryError(msg)
        if not self.path.exists():
            msg = f"Error: File {self.path} not found"
            if self._exit_on_exception:
                print(msg)
                exit(1)
            raise FileNotFoundError(msg)
        registry["CURRENT_FILE"] = str(self.path.resolve())
        self.window = int(registry.get_if_none(window, "WINDOW"))
        self.overlap = int(registry.get("OVERLAP", 0))
        # Ensure that we get a valid current line by using the setter
        self._first_line = 0
        self.first_line = int(
            registry.get_if_none(
                first_line,
                "FIRST_LINE",
                0,
            )
        )
        self.offset_multiplier = 1 / 6
        self._original_text = self.text
        self._original_first_line = self.first_line

    @property
    def first_line(self) -> int:
        return self._first_line

    @first_line.setter
    def first_line(self, value: Union[int, float]):
        self._original_first_line = self.first_line
        value = int(value)
        self._first_line = max(0, min(value, self.n_lines - 1 - self.window))
        registry["FIRST_LINE"] = self.first_line

    @property
    def text(self) -> str:
        return self.path.read_text()

    @text.setter
    def text(self, new_text: str):
        self._original_text = self.text
        self.path.write_text(new_text)

    @property
    def n_lines(self) -> int:
        return len(self.text.splitlines())

    @property
    def line_range(self) -> Tuple[int, int]:
        """Return first and last line (inclusive) of the display window, such
        that exactly `window` many lines are displayed.
        This means `line_range[1] - line_range[0] == window-1` as long as there are
        at least `window` lines in the file. `first_line` does the handling
        of making sure that we don't go out of bounds.
        """
        return self.first_line, min(self.first_line + self.window - 1, self.n_lines - 1)

    def get_window_text(
        self, *, line_numbers: bool = False, status_line: bool = False, pre_post_line: bool = False
    ) -> str:
        start_line, end_line = self.line_range
        lines = self.text.splitlines()[start_line : end_line + 1]
        out_lines = []
        if status_line:
            out_lines.append(f"[File: {self.path} ({self.n_lines} lines total)]")
        if pre_post_line:
            if start_line > 0:
                out_lines.append(f"({start_line} more lines above)")
        if line_numbers:
            out_lines.extend(f"{i + start_line + 1}:{line}" for i, line in enumerate(lines))
        else:
            out_lines.extend(lines)
        if pre_post_line:
            if end_line < self.n_lines - 1:
                out_lines.append(f"({self.n_lines - end_line - 1} more lines below)")
        return "\n".join(out_lines)

    def set_window_text(
        self, new_text: str, *, line_range: Optional[Tuple[int, int]] = None
    ) -> None:
        """Replace the text in the current display window with a new string.

        Args:
            line_range: Range of lines to replace.
                If None, we use the current window.
        """
        text = self.text.splitlines()
        if line_range is not None:
            start, stop = line_range
        else:
            start, stop = self.line_range
        # Preserve trailing newline if present
        ends_with_newline = new_text.endswith('\n')
        new_lines = new_text.splitlines()
        if ends_with_newline:
            new_lines.append('')
        text[start : stop + 1] = new_lines
        self.text = "\n".join(text)

    def replace_in_window(
        self,
        search: str,
        replace: str,
        *,
        reset_first_line: str = "top",
    ) -> "ReplacementInfo":
        """Search and replace in the window.

        Args:
            search: The string to search for (can be multi-line).
            replace: The string to replace it with (can be multi-line).
            reset_first_line: If "keep", we keep the current line. Otherwise, we
                `goto` the line where the replacement started with this mode.
        """
        window_text = self.get_window_text()
        # Update line number
        index = window_text.find(search)
        if index == -1:
            if self._exit_on_exception:
                print(f"Error: Text not found: {search}")
                exit(1)
            raise TextNotFound
        window_start_line, _ = self.line_range
        replace_start_line = window_start_line + len(window_text[:index].splitlines())
        new_window_text = window_text.replace(search, replace)
        self.set_window_text(new_window_text)
        if reset_first_line == "keep":
            pass
        else:
            self.goto(replace_start_line, mode=reset_first_line)
        return ReplacementInfo(
            first_replaced_line=replace_start_line,
            n_search_lines=len(search.splitlines()) + 1,
            n_replace_lines=len(replace.splitlines()) + 1,
            n_replacements=1,
        )

    def replace(self, search: str, replace: str, *, reset_first_line: str = "top") -> "ReplacementInfo":
        indices = list(_find_all(self.text, search))
        if not indices:
            if self._exit_on_exception:
                print(f"Error: Text not found: {search}")
                exit(1)
            raise TextNotFound
        replace_start_line = len(self.text[: indices[0]].splitlines())
        new_text = self.text.replace(search, replace)
        self.text = new_text
        if reset_first_line == "keep":
            pass
        else:
            self.goto(replace_start_line, mode=reset_first_line)
        return ReplacementInfo(
            first_replaced_line=replace_start_line,
            n_search_lines=len(search.splitlines()) + 1,
            n_replace_lines=len(replace.splitlines()) + 1,
            n_replacements=len(indices),
        )

    def print_window(self, *, line_numbers: bool = True, status_line: bool = True, pre_post_line: bool = True):
        print(self.get_window_text(line_numbers=line_numbers, status_line=status_line, pre_post_line=pre_post_line))

    def goto(self, line: int, mode: str = "top"):
        if mode == "top":
            self.first_line = line - self.window * self.offset_multiplier
        else:
            raise NotImplementedError

    def scroll(self, n_lines: int):
        if n_lines > 0:
            self.first_line += n_lines - self.overlap
        elif n_lines < 0:
            self.first_line += n_lines + self.overlap

    def undo_edit(self):
        self.text = self._original_text
        self.first_line = self._original_first_line

    def insert(self, text: str, line: Optional[int] = None, *, reset_first_line: str = "top") -> "InsertInfo":
        if line is None:
            if self.text.endswith("\n"):
                line = self.n_lines
                new_text = self.text + text
            else:
                line = self.n_lines + 1
                new_text = self.text + "\n" + text
        elif line < 0:
            new_text = text + "\n" + self.text
        else:
            _lines = self.text.splitlines()
            lines = _lines[: line + 1] + text.splitlines() + _lines[line + 1 :]
            new_text = "\n".join(lines)
        self.text = new_text
        if reset_first_line == "keep":
            pass
        else:
            self.goto(line, mode=reset_first_line)
        return InsertInfo(first_inserted_line=line, n_lines_added=len(text.splitlines()))
