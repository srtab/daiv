import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

from tree_sitter import Parser, Point
from tree_sitter_language_pack import SupportedLanguage, get_language

from codebase.conf import settings

from .models import (
    ClassElement,
    File,
    Folder,
    FunctionElement,
    InterfaceElement,
    MethodElement,
    ModuleElement,
    Repository,
)

logger = logging.getLogger("daiv.codebase.graphs")

SUPPORTED_LANGUAGES: list[SupportedLanguage] = ["python", "javascript", "typescript", "java", "go", "ruby"]


class CodeImporter:
    def __init__(self):
        self.supported_languages = SUPPORTED_LANGUAGES
        self.parsers: dict[SupportedLanguage, Parser] = {}
        self.queries: dict[SupportedLanguage, str] = {}
        self._init_parsers()
        self._load_queries()

    def _init_parsers(self):
        """
        Initialize tree-sitter parsers for each supported language.
        """
        for lang in self.supported_languages:
            try:
                self.parsers[lang] = Parser(language=get_language(lang))
                logger.info("Initialized parser for %s", lang)
            except Exception as e:
                logger.warning("Failed to initialize parser for %s: %s", lang, e)

    def _load_queries(self):
        """
        Load tree-sitter queries for each supported language.
        """
        self.queries = {
            "python": """(module (expression_statement (assignment left: (identifier) @name) @definition.constant))

(class_definition
  name: (identifier) @name) @definition.class

(function_definition
  name: (identifier) @name) @definition.function

(call
  function: [
      (identifier) @name
      (attribute
        attribute: (identifier) @name)
  ]) @reference.call"""
        }

    def detect_language(self, file_path: Path) -> SupportedLanguage | None:
        """
        Detect language based on file extension.
        """
        ext_to_lang: dict[str, SupportedLanguage] = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".java": "java",
            ".go": "go",
            ".rb": "ruby",
        }
        return ext_to_lang.get(file_path.suffix.lower())

    def import_repository(self, repo_path: Path, repo_id: str, repo_name: str, repo_url: str):
        """
        Import a repository into the graph database.
        """
        if repo := Repository.nodes.get_or_none(repo_id=repo_id):
            logger.info("Repository node for %s already exists", repo_name)
        else:
            repo = Repository(repo_id=repo_id, name=repo_name, url=repo_url, client=settings.CLIENT).save()
            logger.info("Created repository node for %s", repo_name)

        all_files: list[Path] = []
        for lang in self.supported_languages:
            if lang == "python":
                all_files.extend(repo_path.glob("**/*.py"))
            elif lang == "javascript":
                all_files.extend(repo_path.glob("**/*.js"))
            elif lang == "typescript":
                all_files.extend(repo_path.glob("**/*.ts"))
            elif lang == "java":
                all_files.extend(repo_path.glob("**/*.java"))
            elif lang == "go":
                all_files.extend(repo_path.glob("**/*.go"))
            elif lang == "ruby":
                all_files.extend(repo_path.glob("**/*.rb"))

        # Process files in parallel
        with ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
            executor.map(lambda file_path: self.process_file(repo_path, file_path, repo), all_files)

        logger.info("Completed import of repository %s", repo_name)
        return repo

    def process_file(self, repo_path: Path, file_path: Path, repo: Repository):
        """
        Process a single file and extract code elements.
        """
        lang = self.detect_language(file_path)

        if not lang or lang not in self.parsers:
            logger.warning("Unsupported language for file %s", file_path)
            return

        parent_folder = None
        for index, part in enumerate(file_path.relative_to(repo_path).parts[:-1]):
            if folder := Folder.nodes.get_or_none(path=part):
                logger.info("Folder node for %s already exists", part)
            else:
                folder = Folder(path=part, name=part).save()
                logger.info("Created folder node for %s", part)

                if index == 0:
                    repo.folders.connect(folder)
                else:
                    folder.parent.connect(parent_folder)
            parent_folder = folder

        if file_node := File.nodes.get_or_none(path=file_path.relative_to(repo_path)):
            logger.info("File node for %s already exists", file_path)
        else:
            file_node = File(
                path=file_path.relative_to(repo_path), name=file_path.name, extension=file_path.suffix
            ).save()
            file_node.folder.connect(folder)
            logger.info("Created file node for %s", file_path)

        with file_path.open("rb") as f:
            content = f.read()

        language = get_language(lang)
        try:
            tree = self.parsers[lang].parse(content)
            lang_query = language.query(self.queries[lang])
            matches = lang_query.matches(tree.root_node)

            for _, captures in matches:
                name = cast("bytes", captures["name"][0].text).decode("utf-8")

                if "definition.class" in captures and captures["definition.class"][0].start_point.column == 0:
                    start_point = captures["definition.class"][0].start_point
                    end_point = captures["definition.class"][0].end_point
                    self._create_class_definition(file_node, name, start_point, end_point)
                elif "definition.function" in captures and captures["definition.function"][0].start_point.column == 0:
                    start_point = captures["definition.function"][0].start_point
                    end_point = captures["definition.function"][0].end_point
                    self._create_function_definition(file_node, name, start_point, end_point)
                elif "definition.interface" in captures and captures["definition.interface"][0].start_point.column == 0:
                    start_point = captures["definition.interface"][0].start_point
                    end_point = captures["definition.interface"][0].end_point
                    self._create_interface_definition(file_node, name, start_point, end_point)
                elif "definition.method" in captures and captures["definition.method"][0].start_point.column == 0:
                    start_point = captures["definition.method"][0].start_point
                    end_point = captures["definition.method"][0].end_point
                    self._create_method_definition(file_node, name, start_point, end_point)
                elif "definition.module" in captures and captures["definition.module"][0].start_point.column == 0:
                    start_point = captures["definition.module"][0].start_point
                    end_point = captures["definition.module"][0].end_point
                    self._create_module_definition(file_node, name, start_point, end_point)

            for _, captures in matches:
                name = cast("bytes", captures["name"][0].text).decode("utf-8")

                if "reference.call" in captures:
                    start_point = captures["reference.call"][0].start_point
                    end_point = captures["reference.call"][0].end_point
                    self._create_call_reference(file_node, name, start_point, end_point)
                elif "reference.class" in captures:
                    start_point = captures["reference.class"][0].start_point
                    end_point = captures["reference.class"][0].end_point
                    self._create_class_reference(file_node, name, start_point, end_point)
                elif "reference.implementation" in captures:
                    start_point = captures["reference.implementation"][0].start_point
                    end_point = captures["reference.implementation"][0].end_point
                    self._create_implementation_reference(file_node, name, start_point, end_point)
        except Exception:
            logger.exception("Error processing file %s", file_path)
        else:
            logger.info("Processed file %s", file_path)

    def _create_class_definition(self, file_node: File, name: str, start_point: Point, end_point: Point):
        """Create a class definition node and connect it to the file."""
        class_node = ClassElement(name=name).save()

        file_node.defines_class.connect(
            class_node,
            {
                "start_position": {"line": start_point[0], "column": start_point[1]},
                "end_position": {"line": end_point[0], "column": end_point[1]},
                "tag": "@definition.class",
            },
        )
        return class_node

    def _create_function_definition(self, file_node: File, name: str, start_point: Point, end_point: Point):
        """Create a function definition node and connect it to the file."""
        function_node = FunctionElement(name=name).save()

        file_node.defines_function.connect(
            function_node,
            {
                "start_position": {"line": start_point[0], "column": start_point[1]},
                "end_position": {"line": end_point[0], "column": end_point[1]},
                "tag": "@definition.function",
            },
        )
        return function_node

    def _create_interface_definition(self, file_node: File, name: str, start_point: Point, end_point: Point):
        """Create an interface definition node and connect it to the file."""
        interface_node = InterfaceElement(name=name).save()
        file_node.defines_interface.connect(
            interface_node,
            {
                "start_position": {"line": start_point[0], "column": start_point[1]},
                "end_position": {"line": end_point[0], "column": end_point[1]},
                "tag": "@definition.interface",
            },
        )
        return interface_node

    def _create_method_definition(self, file_node: File, name: str, start_point: Point, end_point: Point):
        """Create a method definition node and connect it to the file."""
        method_node = MethodElement(name=name).save()
        file_node.defines_method.connect(
            method_node,
            {
                "start_position": {"line": start_point[0], "column": start_point[1]},
                "end_position": {"line": end_point[0], "column": end_point[1]},
                "tag": "@definition.method",
            },
        )
        return method_node

    def _create_module_definition(self, file_node: File, name: str, start_point: Point, end_point: Point):
        """Create a module definition node and connect it to the file."""
        module_node = ModuleElement(name=name).save()
        file_node.defines_module.connect(
            module_node,
            {
                "start_position": {"line": start_point[0], "column": start_point[1]},
                "end_position": {"line": end_point[0], "column": end_point[1]},
                "tag": "@definition.module",
            },
        )
        return module_node

    def _create_call_reference(self, file_node: File, name: str, start_point: Point, end_point: Point):
        """Create a function/method call reference using NeoModel ORM."""
        # Try to find the function being called
        if function := FunctionElement.nodes.get_or_none(name=name):
            file_node.refers_call.connect(
                function,
                {
                    "start_position": {"line": start_point[0], "column": start_point[1]},
                    "end_position": {"line": end_point[0], "column": end_point[1]},
                    "tag": "@reference.call",
                },
            )

        # If not found as a function, try to find it as a method
        elif method := MethodElement.nodes.get_or_none(name=name):
            file_node.refers_call.connect(
                method,
                {
                    "start_position": {"line": start_point[0], "column": start_point[1]},
                    "end_position": {"line": end_point[0], "column": end_point[1]},
                    "tag": "@reference.call",
                },
            )

    def _create_class_reference(self, file_node: File, name: str, start_point: Point, end_point: Point):
        """Create a class reference using NeoModel ORM."""
        # Try to find the class being referenced
        if class_node := ClassElement.nodes.get_or_none(name=name):
            file_node.refers_class.connect(
                class_node,
                {
                    "start_position": {"line": start_point[0], "column": start_point[1]},
                    "end_position": {"line": end_point[0], "column": end_point[1]},
                    "tag": "@reference.class",
                },
            )

    def _create_implementation_reference(self, file_node: File, name: str, start_point: Point, end_point: Point):
        """Create an interface implementation reference using NeoModel ORM."""
        # Try to find the interface being implemented
        if interface_node := InterfaceElement.nodes.get_or_none(name=name):
            file_node.refers_implementation.connect(
                interface_node,
                {
                    "start_position": {"line": start_point[0], "column": start_point[1]},
                    "end_position": {"line": end_point[0], "column": end_point[1]},
                    "tag": "@reference.implementation",
                },
            )


def import_repositories(repo_paths: list[str]):
    """Import multiple repositories."""
    importer = CodeImporter()

    for repo_path in repo_paths:
        importer.import_repository(repo_path)


if __name__ == "__main__":
    # Example usage
    repos_to_import = [
        "/path/to/repo1",
        "/path/to/repo2",
        # Add more repositories as needed
    ]

    import_repositories(repos_to_import)
