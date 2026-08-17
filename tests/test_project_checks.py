import unittest
from unittest import mock

from tools import check_project


class JavaScriptBindingCheckTests(unittest.TestCase):
    def test_missing_shared_callable_import_is_reported(self):
        source = """
            import { post } from './core.js';
            function removeApp() {
              post('/prepare');
              return del('/api/apps/one');
            }
        """
        self.assertEqual(
            check_project.find_unbound_shared_calls(
                source, {"post", "del"}),
            ["del"],
        )

    def test_import_alias_local_declaration_comments_and_strings_are_allowed(self):
        source = """
            import { del as remove, post } from './core.js';
            function del() { return remove('/local'); }
            // missing('/comment-only')
            const example = "missing('/string-only')";
            post('/prepare');
            del();
        """
        self.assertEqual(
            check_project.find_unbound_shared_calls(
                source, {"post", "del", "missing"}),
            [],
        )

    def test_core_callable_exports_include_functions_and_arrow_functions(self):
        source = """
            export function regular() {}
            export async function later() {}
            export const arrow = value => value;
            export const grouped = (value, other) => value + other;
            export const data = {};
        """
        self.assertEqual(
            check_project.javascript_exported_callables(source),
            {"regular", "later", "arrow", "grouped"},
        )

    def test_project_modules_have_no_unbound_core_calls(self):
        detail = check_project.check_javascript_bindings()
        self.assertIn("公共可调用导出", detail)

    def test_windows_launcher_check_does_not_invoke_macos_tools(self):
        with mock.patch.object(check_project.os, "name", "nt"), \
                mock.patch.object(check_project, "command_output") as command:
            detail = check_project.check_shell_and_plist()
        self.assertIn("Windows", detail)
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
