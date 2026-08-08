import ast
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def parse_source(relative_path):
    return ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))


def find_class(tree, name):
    return next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)


def find_function(tree, name):
    return next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)


def find_assignment(nodes, name):
    for node in nodes:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return node.value
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return node.value
    raise AssertionError(f"Assignment to {name!r} was not found")


class CollectionConditionDefaultTests(unittest.TestCase):
    def setUp(self):
        self.schemas = parse_source("backend/schemas.py")

    def assert_class_string_default(self, class_name, field_name, expected):
        class_node = find_class(self.schemas, class_name)
        self.assertEqual(ast.literal_eval(find_assignment(class_node.body, field_name)), expected)

    def test_collection_item_create_schema_defaults_to_mint(self):
        self.assert_class_string_default("CollectionItemCreate", "condition", "Mint")

    def test_collection_item_model_defaults_to_mint(self):
        model = find_class(parse_source("backend/models.py"), "CollectionItem")
        column = find_assignment(model.body, "condition")
        default = next(keyword.value for keyword in column.keywords if keyword.arg == "default")
        self.assertEqual(ast.literal_eval(default), "Mint")

    def test_csv_import_blank_condition_defaults_to_mint(self):
        parser = find_function(parse_source("backend/api/collection.py"), "_parse_import_row")
        condition = find_assignment(parser.body, "condition")
        string_constants = [node.value for node in ast.walk(condition) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        self.assertEqual(string_constants.count("Mint"), 2)
        self.assertNotIn("NM", string_constants)

    def test_trade_incoming_create_schema_defaults_to_mint(self):
        self.assert_class_string_default("TradeIncomingItemCreate", "condition", "Mint")

    def test_trade_incoming_update_schema_defaults_to_mint(self):
        self.assert_class_string_default("TradeIncomingItemUpdate", "condition", "Mint")

    def test_trade_create_service_defaults_new_inventory_to_mint(self):
        prepare = find_function(parse_source("backend/api/trades.py"), "_prepare_incoming_card")
        condition = find_assignment(prepare.body, "condition")
        self.assertEqual(ast.literal_eval(condition.values[1]), "Mint")

    def test_trade_update_service_defaults_new_inventory_to_mint(self):
        update = find_function(parse_source("backend/api/trades.py"), "update_trade")
        new_condition = next(
            node.value
            for node in ast.walk(update)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "new_condition" for target in node.targets)
            and isinstance(node.value, ast.IfExp)
            and any(isinstance(child, ast.Constant) and child.value == "Mint" for child in ast.walk(node.value))
        )
        requested_default = new_condition.body
        self.assertEqual(ast.literal_eval(requested_default.values[1]), "Mint")

        new_inventory_conditions = [
            keyword.value
            for node in ast.walk(update)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_merge_locked_collection_item"
            for keyword in node.keywords
            if keyword.arg == "condition"
            and isinstance(keyword.value, ast.BoolOp)
            and isinstance(keyword.value.values[0], ast.Name)
            and keyword.value.values[0].id == "new_condition"
        ]
        self.assertEqual(len(new_inventory_conditions), 1)
        self.assertEqual(ast.literal_eval(new_inventory_conditions[0].values[1]), "Mint")


if __name__ == "__main__":
    unittest.main()
