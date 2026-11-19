import json

import requests_mock
from django.test import TestCase

from core.factories import BatchFactory, TokenFactory, UserFactory
from core.models import Batch, BatchCommand
from core.parsers.base import ParserException
from core.parsers.csv import CSVCommandParser
from core.parsers.v1 import V1CommandParser
from core.placeholders import is_placeholder
from core.templatetags.quickstatements import datavalue_display
from core.tests.test_api import ApiMocker

ISSUE_EXAMPLE = """
CREATE\t{Person_A}
LAST\tLen\t"Alice"
LAST\tP31\tQ5
CREATE\t{Person_B}
LAST\tLen\t"Bob"
LAST\tP31\tQ5
LAST\tP369\t{Person_A}
{Person_A}\tP369\t{Person_B}
"""


class PlaceholderSyntaxTests(TestCase):
    def test_is_placeholder(self):
        self.assertTrue(is_placeholder("{Person_A}"))
        self.assertTrue(is_placeholder("{item-1}"))
        self.assertTrue(is_placeholder("{a}"))
        self.assertFalse(is_placeholder("{}"))
        self.assertFalse(is_placeholder("{a b}"))
        self.assertFalse(is_placeholder("{a"))
        self.assertFalse(is_placeholder("Q1"))
        self.assertFalse(is_placeholder("LAST"))
        self.assertFalse(is_placeholder(None))
        self.assertFalse(is_placeholder({"type": "string"}))


class V1PlaceholderParserTests(TestCase):
    def setUp(self):
        self.parser = V1CommandParser()

    def parse(self, text):
        return list(self.parser.parse(text))

    def test_create_with_placeholder(self):
        data = self.parser.parse_command("CREATE\t{Person_A}")
        self.assertEqual(data, {"action": "create", "type": "item", "placeholder": "{Person_A}"})

    def test_create_without_placeholder_is_unchanged(self):
        data = self.parser.parse_command("CREATE")
        self.assertEqual(data, {"action": "create", "type": "item"})

    def test_create_property_with_placeholder(self):
        data = self.parser.parse_command("CREATE_PROPERTY\tstring\t{prop}")
        self.assertEqual(
            data,
            {"action": "create", "type": "property", "data": "string", "placeholder": "{prop}"},
        )

    def test_create_with_invalid_placeholder(self):
        for raw in ["CREATE\tQ123", "CREATE\t{a b}", "CREATE\t{A}\t{B}"]:
            with self.assertRaises(ParserException):
                self.parser.parse_command(raw)

    def test_placeholder_in_all_positions(self):
        data = self.parser.parse_command("{Item_A}\tP1\t{Item_B}\tP2\t{Item_C}\tS3\t{Item_D}")
        self.assertEqual(data["entity"], {"type": "item", "id": "{Item_A}"})
        self.assertEqual(data["value"], {"type": "wikibase-entityid", "value": "{Item_B}"})
        self.assertEqual(
            data["qualifiers"],
            [{"property": "P2", "value": {"type": "wikibase-entityid", "value": "{Item_C}"}}],
        )
        self.assertEqual(
            data["references"],
            [[{"property": "P3", "value": {"type": "wikibase-entityid", "value": "{Item_D}"}}]],
        )

    def test_placeholder_case_is_kept(self):
        data = self.parser.parse_command("-{lower_case}\tP1\tQ1")
        self.assertEqual(data["action"], "remove")
        self.assertEqual(data["entity"]["id"], "{lower_case}")
        data = self.parser.parse_command('{lower_case}\tLen\t"label"')
        self.assertEqual(data["item"], "{lower_case}")

    def test_placeholder_in_switch_value(self):
        data = self.parser.parse_command("SWITCH_VALUE\t{a}\tP1\t{b}\t{c}")
        self.assertEqual(data["entity"]["id"], "{a}")
        self.assertEqual(data["value"]["value"], "{b}")
        self.assertEqual(data["value_switch"]["value"], "{c}")

    def test_placeholder_not_allowed_as_property_merge_or_statement_id(self):
        for raw in ["Q1\t{p}\tQ2", "MERGE\t{a}\tQ2", "-STATEMENT\t{a}$1234"]:
            with self.assertRaises(ParserException):
                self.parser.parse_command(raw)

    def test_issue_example_parses(self):
        commands = self.parse(ISSUE_EXAMPLE)
        self.assertEqual(len(commands), 8)
        for command in commands:
            self.assertEqual(command.status, BatchCommand.STATUS_INITIAL, command.message)
        self.assertEqual(commands[0].placeholder, "{Person_A}")
        self.assertEqual(commands[3].placeholder, "{Person_B}")
        self.assertEqual(commands[6].placeholders_used(), ["{Person_A}"])
        self.assertEqual(commands[7].placeholders_used(), ["{Person_A}", "{Person_B}"])

    def test_use_before_definition(self):
        commands = self.parse("CREATE\t{A}\nLAST\tP1\t{B}\nCREATE\t{B}")
        self.assertEqual(commands[0].status, BatchCommand.STATUS_INITIAL)
        self.assertEqual(commands[1].status, BatchCommand.STATUS_ERROR)
        self.assertIn("{B} is used before it is defined", commands[1].message)
        self.assertEqual(commands[2].status, BatchCommand.STATUS_INITIAL)

    def test_never_defined(self):
        commands = self.parse("Q1\tP1\t{nowhere}")
        self.assertEqual(commands[0].status, BatchCommand.STATUS_ERROR)
        self.assertIn("{nowhere}", commands[0].message)

    def test_duplicate_definition(self):
        commands = self.parse("CREATE\t{A}\nCREATE\t{A}")
        self.assertEqual(commands[0].status, BatchCommand.STATUS_INITIAL)
        self.assertEqual(commands[1].status, BatchCommand.STATUS_ERROR)
        self.assertIn("already defined by command #0", commands[1].message)

    def test_placeholders_are_case_sensitive(self):
        commands = self.parse("CREATE\t{A}\nQ1\tP1\t{a}")
        self.assertEqual(commands[1].status, BatchCommand.STATUS_ERROR)

    def test_invalid_create_does_not_define(self):
        commands = self.parse("CREATE\t{A}\tjunk\nQ1\tP1\t{A}")
        self.assertEqual(commands[0].status, BatchCommand.STATUS_ERROR)
        self.assertEqual(commands[1].status, BatchCommand.STATUS_ERROR)

    def test_pipe_syntax(self):
        commands = self.parse("CREATE|{A}||Q1|P1|{A}")
        self.assertEqual(commands[0].placeholder, "{A}")
        self.assertEqual(commands[1].status, BatchCommand.STATUS_INITIAL)
        self.assertEqual(commands[1].value_value, "{A}")

    def test_quoted_braces_are_strings(self):
        commands = self.parse('Q1\tP1\t"{A}"')
        self.assertEqual(commands[0].status, BatchCommand.STATUS_INITIAL)
        self.assertEqual(commands[0].value_dict, {"type": "string", "value": "{A}"})


class CSVPlaceholderParserTests(TestCase):
    def parse(self, text):
        return list(CSVCommandParser().parse(text))

    def test_placeholder_in_qid_and_value(self):
        commands = self.parse("qid,Len,P369\n{Alice},Alice,\n{Bob},Bob,{Alice}\n")
        self.assertEqual(
            commands[0].json, {"action": "create", "type": "item", "placeholder": "{Alice}"}
        )
        self.assertEqual(commands[1].json["item"], "LAST")
        self.assertEqual(commands[2].placeholder, "{Bob}")
        self.assertEqual(commands[4].json["entity"]["id"], "LAST")
        self.assertEqual(
            commands[4].json["value"], {"type": "wikibase-entityid", "value": "{Alice}"}
        )
        for command in commands:
            self.assertEqual(command.status, BatchCommand.STATUS_INITIAL, command.message)

    def test_forward_reference_is_an_error(self):
        commands = self.parse("qid,Len,P369\n{Alice},Alice,{Bob}\n{Bob},Bob,\n")
        statement = next(c for c in commands if c.json.get("property") == "P369")
        self.assertEqual(statement.status, BatchCommand.STATUS_ERROR)
        self.assertIn("{Bob} is used before it is defined", statement.message)

    def test_placeholder_as_existing_entity(self):
        commands = self.parse("qid,P369\n{Alice},Q1\nQ2,{Alice}\n")
        self.assertEqual(commands[2].json["entity"]["id"], "Q2")
        self.assertEqual(commands[2].value_value, "{Alice}")
        self.assertEqual(commands[2].status, BatchCommand.STATUS_INITIAL)


class PlaceholderProcessingTests(TestCase):
    def setUp(self):
        self.api_mocker = ApiMocker()
        self.user = UserFactory(username="user")
        self.token = TokenFactory(user=self.user)

    def parse(self, text, **extra):
        return BatchFactory.load_from_parser(
            V1CommandParser(), "Test", "user", text, wikibase=self.api_mocker.wikibase, **extra
        )

    def mock_basics(self, mocker, properties=("P31", "P369")):
        self.api_mocker.is_autoconfirmed(mocker)
        self.api_mocker.wikidata_property_data_types(mocker)
        for prop in properties:
            self.api_mocker.property_data_type(mocker, prop, "wikibase-item")

    def mock_creations(self, mocker, *item_ids):
        mocker.post(
            self.api_mocker.wikibase_url("/entities/items"),
            [{"json": {"id": item_id}, "status_code": 200} for item_id in item_ids],
        )
        for item_id in item_ids:
            self.api_mocker.item_empty(mocker, item_id)
            self.api_mocker.patch_item_successful(mocker, item_id, {"id": item_id})

    def requests(self, mocker, method):
        return [r for r in mocker.request_history if r.method == method]

    @requests_mock.Mocker()
    def test_issue_example_without_combining(self, mocker):
        self.mock_basics(mocker)
        self.mock_creations(mocker, "Q100", "Q200")

        batch = self.parse(ISSUE_EXAMPLE)
        batch.run()
        self.assertEqual(batch.status, Batch.STATUS_DONE)
        commands = list(batch.commands())
        for command in commands:
            self.assertEqual(command.status, BatchCommand.STATUS_DONE, command.message)

        self.assertEqual(commands[0].entity_id, "Q100")
        self.assertEqual(commands[3].entity_id, "Q200")
        self.assertEqual(commands[6].entity_id, "Q200")
        self.assertEqual(commands[6].value_value, "Q100")
        self.assertEqual(commands[7].entity_id, "Q100")
        self.assertEqual(commands[7].value_value, "Q200")

        patches = self.requests(mocker, "PATCH")
        self.assertTrue(patches[-1].url.endswith("/entities/items/Q100"))
        self.assertIn('"Q200"', json.dumps(patches[-1].json()))
        self.assertNotIn("{Person", json.dumps([p.json() for p in patches]))

    @requests_mock.Mocker()
    def test_issue_example_with_combining(self, mocker):
        self.mock_basics(mocker)
        self.mock_creations(mocker, "Q100", "Q200")

        batch = self.parse(ISSUE_EXAMPLE, combine_commands=True)
        batch.run()
        self.assertEqual(batch.status, Batch.STATUS_DONE)
        commands = list(batch.commands())
        for command in commands:
            self.assertEqual(command.status, BatchCommand.STATUS_DONE, command.message)

        posts = self.requests(mocker, "POST")
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0].json()["item"]["labels"], {"en": "Alice"})
        # Bob is created already linked to Alice
        bob = posts[1].json()["item"]
        self.assertEqual(bob["labels"], {"en": "Bob"})
        self.assertEqual(bob["statements"]["P369"][0]["value"]["content"], "Q100")
        # and Alice is linked to Bob afterwards
        patches = self.requests(mocker, "PATCH")
        self.assertEqual(len(patches), 1)
        self.assertTrue(patches[0].url.endswith("/entities/items/Q100"))
        self.assertIn('"Q200"', json.dumps(patches[0].json()))

        self.assertEqual(commands[0].entity_id, "Q100")
        self.assertEqual(commands[3].entity_id, "Q200")
        self.assertEqual(commands[7].entity_id, "Q100")

    @requests_mock.Mocker()
    def test_placeholder_entity_combines_with_its_creation(self, mocker):
        self.mock_basics(mocker)
        self.mock_creations(mocker, "Q100")

        batch = self.parse('CREATE\t{A}\n{A}\tLen\t"Alice"\nLAST\tP31\tQ5', combine_commands=True)
        batch.run()
        commands = list(batch.commands())
        for command in commands:
            self.assertEqual(command.status, BatchCommand.STATUS_DONE, command.message)
            self.assertEqual(command.entity_id, "Q100")
        posts = self.requests(mocker, "POST")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].json()["item"]["labels"], {"en": "Alice"})
        self.assertIn("P31", posts[0].json()["item"]["statements"])
        self.assertEqual(self.requests(mocker, "PATCH"), [])

    @requests_mock.Mocker()
    def test_self_reference_splits_combined_creation(self, mocker):
        self.mock_basics(mocker, properties=("P31", "P1659"))
        self.mock_creations(mocker, "Q100")

        batch = self.parse("CREATE\t{A}\nLAST\tP31\tQ5\nLAST\tP1659\t{A}", combine_commands=True)
        batch.run()
        commands = list(batch.commands())
        for command in commands:
            self.assertEqual(command.status, BatchCommand.STATUS_DONE, command.message)
        posts = self.requests(mocker, "POST")
        self.assertEqual(len(posts), 1)
        self.assertNotIn("P1659", posts[0].json()["item"]["statements"])
        patches = self.requests(mocker, "PATCH")
        self.assertEqual(len(patches), 1)
        self.assertTrue(patches[0].url.endswith("/entities/items/Q100"))
        self.assertIn('"Q100"', json.dumps(patches[0].json()))
        self.assertEqual(commands[2].entity_id, "Q100")
        self.assertEqual(commands[2].value_value, "Q100")

    @requests_mock.Mocker()
    def test_failed_creation_fails_uses(self, mocker):
        self.mock_basics(mocker)
        self.api_mocker.create_item_failed_server(mocker)
        self.api_mocker.item_empty(mocker, "Q1")
        self.api_mocker.patch_item_successful(mocker, "Q1", {"id": "Q1"})

        batch = self.parse("CREATE\t{A}\nQ1\tP369\t{A}\nQ1\tP31\tQ5\n{A}\tP31\tQ5")
        batch.run()
        self.assertEqual(batch.status, Batch.STATUS_DONE)
        commands = list(batch.commands())
        self.assertEqual(commands[0].status, BatchCommand.STATUS_ERROR)
        self.assertEqual(commands[1].status, BatchCommand.STATUS_ERROR)
        self.assertEqual(commands[1].error, BatchCommand.Error.PLACEHOLDER_NOT_EVALUATED)
        self.assertIn("{A}", commands[1].message)
        self.assertEqual(commands[2].status, BatchCommand.STATUS_DONE)
        self.assertEqual(commands[3].status, BatchCommand.STATUS_ERROR)
        self.assertEqual(commands[3].error, BatchCommand.Error.PLACEHOLDER_NOT_EVALUATED)

    @requests_mock.Mocker()
    def test_failed_combined_creation_fails_uses(self, mocker):
        self.mock_basics(mocker)
        self.api_mocker.create_item_failed_server(mocker)

        batch = self.parse("CREATE\t{A}\nLAST\tP31\tQ5\n{A}\tP31\tQ6", combine_commands=True)
        batch.run()
        commands = list(batch.commands())
        for command in commands:
            self.assertEqual(command.status, BatchCommand.STATUS_ERROR)
        # the third command was combined into the creation, so it fails with it
        self.assertEqual(len(self.requests(mocker, "POST")), 1)

    @requests_mock.Mocker()
    def test_failed_creation_blocks_on_errors(self, mocker):
        self.mock_basics(mocker)
        self.api_mocker.create_item_failed_server(mocker)

        batch = self.parse("CREATE\t{A}\nQ1\tP369\t{A}", block_on_errors=True)
        batch.run()
        self.assertEqual(batch.status, Batch.STATUS_BLOCKED)
        commands = list(batch.commands())
        self.assertEqual(commands[0].status, BatchCommand.STATUS_ERROR)
        self.assertEqual(commands[1].status, BatchCommand.STATUS_INITIAL)

    @requests_mock.Mocker()
    def test_restart_keeps_placeholders(self, mocker):
        self.mock_basics(mocker)
        self.api_mocker.item_empty(mocker, "Q1")
        self.api_mocker.patch_item_successful(mocker, "Q1", {"id": "Q1"})

        batch = self.parse("CREATE\t{A}\nQ1\tP369\t{A}")
        # Simulate a previous run that created the item and then stopped
        create = batch.commands()[0]
        create.status = BatchCommand.STATUS_DONE
        create.set_entity_id("Q100")
        create.save()

        self.assertEqual(batch.bound_placeholders(), {"{A}": "Q100"})
        batch.run()
        self.assertEqual(batch.status, Batch.STATUS_DONE)
        command = batch.commands()[1]
        self.assertEqual(command.status, BatchCommand.STATUS_DONE, command.message)
        self.assertEqual(command.value_value, "Q100")
        self.assertEqual(self.requests(mocker, "POST"), [])

    @requests_mock.Mocker()
    def test_rerun_after_unrelated_error(self, mocker):
        self.mock_basics(mocker)
        self.mock_creations(mocker, "Q100")
        mocker.patch(
            self.api_mocker.wikibase_url("/entities/items/Q1"),
            [
                {"json": {"error": "my-error-code"}, "status_code": 500},
                {"json": {"id": "Q1"}, "status_code": 200},
            ],
        )
        self.api_mocker.item_empty(mocker, "Q1")

        batch = self.parse("CREATE\t{A}\nQ1\tP369\t{A}")
        batch.run()
        commands = list(batch.commands())
        self.assertEqual(commands[0].status, BatchCommand.STATUS_DONE)
        self.assertEqual(commands[1].status, BatchCommand.STATUS_ERROR)

        batch.rerun()
        batch.run()
        commands = list(batch.commands())
        self.assertEqual(commands[1].status, BatchCommand.STATUS_DONE, commands[1].message)
        self.assertEqual(commands[1].value_value, "Q100")
        self.assertEqual(len(self.requests(mocker, "POST")), 1)


class PlaceholderDisplayTests(TestCase):
    def command(self, text, index=-1):
        batch = BatchFactory.load_from_parser(V1CommandParser(), "Test", "user", text)
        return list(batch.commands())[index]

    def test_placeholder_value_is_plain_text(self):
        command = self.command("CREATE\t{A}\nQ1\tP1\t{A}")
        html = datavalue_display(command, command.value_dict)
        self.assertEqual(html, '<span class="placeholder">{A}</span>')

    def test_placeholder_is_not_looked_up(self):
        command = self.command("CREATE\t{A}\nCREATE\t{B}\n{A}\tP1\t{B}\tP2\t{A}")
        self.assertEqual(command.related_identifiers_set, {"P1", "P2"})
        self.assertEqual(command.entity_url(), "")
