# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
#  Copyright 2024 Canonical Ltd.
#
#  This program is free software: you can redistribute it and/or modify it
#  under the terms of the GNU Lesser General Public License version 3, as
#  published by the Free Software Foundation.
#
#  This program is distributed in the hope that it will be useful, but WITHOUT
#  ANY WARRANTY; without even the implied warranties of MERCHANTABILITY,
#  SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR PURPOSE.
#  See the GNU Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Abstract service class for assertions."""

from __future__ import annotations

import abc
import io
import json
import os
import pathlib
import subprocess
import tempfile
from typing import Any

import craft_cli
import tabulate
import yaml
from craft_application.errors import CraftValidationError
from craft_application.services import base
from craft_application.util import safe_yaml_load
from typing_extensions import override

from snapcraft import const, errors, models, store, utils


class Assertion(base.AppService):
    """Abstract service for interacting with assertions."""

    @override
    def setup(self) -> None:
        """Application-specific service setup."""
        self._store_client = store.StoreClientCLI()
        self._editor_cmd = os.getenv("EDITOR", "vi")
        super().setup()

    @property
    @abc.abstractmethod
    def _assertion_name(self) -> str:
        """The lowercase name of the assertion type."""

    @property
    @abc.abstractmethod
    def _editable_assertion_class(self) -> type[models.EditableAssertion]:
        """The type of the editable assertion."""

    @abc.abstractmethod
    def _get_assertions(self, name: str | None = None) -> list[models.Assertion]:
        """Get assertions from the store.

        :param name: The name of the assertion to retrieve. If not provided, all
          assertions are retrieved.

        :returns: A list of assertions.
        """

    @abc.abstractmethod
    def _normalize_assertions(
        self, assertions: list[models.Assertion]
    ) -> tuple[list[str], list[list[Any]]]:
        """Convert a list of assertion models to a tuple of headers and data.

        :param assertions: A list of assertions to normalize.

        :returns: A tuple containing the headers and normalized assertions.
        """

    @abc.abstractmethod
    def _generate_yaml_from_model(self, assertion: models.Assertion) -> str:
        """Generate a multi-line yaml string from an existing assertion.

        This string should contain only user-editable data.

        :param assertion: The assertion to generate a yaml string from.

        :returns: A multi-line yaml string.
        """

    @abc.abstractmethod
    def _generate_yaml_from_template(self, name: str, account_id: str) -> str:
        """Generate a multi-line yaml string of a default assertion.

        This string should contain only user-editable data.

        :param name: The name of the assertion.
        :param account_id: The account ID of the authenticated user.

        :returns: A multi-line yaml string.
        """

    def list_assertions(self, *, output_format: str, name: str | None = None) -> None:
        """List assertions from the store.

        :param output_format: The output format to render.
        :param name: The name of the assertion to list. If not provided, all assertions
          are listed.

        :raises FeatureNotImplemented: If the output format is not supported.
        """
        assertions = self._get_assertions(name)

        if assertions:
            headers, normalized_assertions = self._normalize_assertions(assertions)
            match output_format:
                case const.OutputFormat.json:
                    json_assertions = {
                        f"{self._assertion_name}s": [
                            {
                                header.lower(): value
                                for header, value in zip(headers, assertion)
                            }
                            for assertion in normalized_assertions
                        ]
                    }
                    craft_cli.emit.message(json.dumps(json_assertions, indent=4))
                case const.OutputFormat.table:
                    tabulated_sets = tabulate.tabulate(
                        normalized_assertions,
                        headers=headers,
                        tablefmt="plain",
                    )
                    craft_cli.emit.message(tabulated_sets)
                case _:
                    raise errors.FeatureNotImplemented(
                        msg=f"'--format {output_format}'",
                    )
        else:
            craft_cli.emit.message(f"No {self._assertion_name}s found.")

    def _edit_yaml_file(self, filepath: pathlib.Path) -> models.EditableAssertion:
        """Edit a yaml file and unmarshal it to an editable assertion.

        If the file is not valid, the user is prompted to amend it.

        :param filepath: The path to the yaml file to edit.

        :returns: The edited assertion.
        """
        while True:
            craft_cli.emit.debug(f"Using {self._editor_cmd} to edit file.")
            with craft_cli.emit.pause():
                subprocess.run([self._editor_cmd, filepath], check=True)
            try:
                with filepath.open() as file:
                    data = safe_yaml_load(file)
                edited_assertion = self._editable_assertion_class.from_yaml_data(
                    data=data,
                    # filepath is only shown for pydantic errors and snapcraft should
                    # not expose the temp file name
                    filepath=pathlib.Path(self._assertion_name.replace(" ", "-")),
                )
                return edited_assertion
            except (yaml.YAMLError, CraftValidationError) as err:
                craft_cli.emit.message(f"{err!s}")
                if not utils.confirm_with_user(
                    f"Do you wish to amend the {self._assertion_name}?"
                ):
                    raise errors.SnapcraftError("operation aborted") from err

    def _get_yaml_data(self, name: str, account_id: str) -> str:
        craft_cli.emit.progress(
            f"Requesting {self._assertion_name} '{name}' from the store."
        )

        if assertions := self._get_assertions(name=name):
            yaml_data = self._generate_yaml_from_model(assertions[0])
        else:
            craft_cli.emit.progress(
                f"Creating a new {self._assertion_name} because no existing "
                f"{self._assertion_name} named '{name}' was found for the "
                "authenticated account.",
                permanent=True,
            )
            yaml_data = self._generate_yaml_from_template(
                name=name, account_id=account_id
            )

        return yaml_data

    @staticmethod
    def _write_to_file(yaml_data: str) -> pathlib.Path:
        with tempfile.NamedTemporaryFile() as temp_file:
            filepath = pathlib.Path(temp_file.name)
        craft_cli.emit.trace(f"Writing yaml data to temporary file '{filepath}'.")
        filepath.write_text(yaml_data, encoding="utf-8")
        return filepath

    @staticmethod
    def _remove_temp_file(filepath: pathlib.Path) -> None:
        craft_cli.emit.trace(f"Removing temporary file '{filepath}'.")
        filepath.unlink()

    def edit_assertion(self, *, name: str, account_id: str) -> None:
        """Edit, sign and upload an assertion.

         If the assertion does not exist, a new assertion is created from a template.

        :param name: The name of the assertion to edit.
        :param account_id: The account ID associated with the registries set.
        """
        yaml_data = self._get_yaml_data(name=name, account_id=account_id)
        yaml_file = self._write_to_file(yaml_data)
        original_assertion = self._editable_assertion_class.unmarshal(
            safe_yaml_load(io.StringIO(yaml_data))
        )
        edited_assertion = self._edit_yaml_file(yaml_file)

        if edited_assertion == original_assertion:
            craft_cli.emit.message("No changes made.")
            self._remove_temp_file(yaml_file)
            return

        # TODO: build, sign, and push assertion (#5018)

        self._remove_temp_file(yaml_file)
        craft_cli.emit.message(f"Successfully edited {self._assertion_name} {name!r}.")
        raise errors.FeatureNotImplemented(
            f"Building, signing and uploading {self._assertion_name} is not implemented.",
        )