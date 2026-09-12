"""Idioma de la partida.

El idioma decide **lo que sale**: los mensajes al grupo, los privados y lo que
se le pide al modelo. No decide lo que entra: los parsers aceptan las palabras
de los dos idiomas siempre, porque en un grupo mixto alguien va a contestar
"heal" en una partida en español, y entenderlo no cuesta nada.

Un catálogo es un diccionario por idioma con las **mismas claves**. Si a uno le
falta una, se cae al español en vez de reventar: una frase sin traducir es un
defecto cosmético y no vale tumbar una partida por ella. La paridad de claves
se comprueba en los tests, que es donde toca detectarlo.
"""

from __future__ import annotations

from typing import Literal

Language = Literal["es", "en"]

#: Idioma de referencia: el que tiene todas las claves y al que se cae.
DEFAULT_LANGUAGE: Language = "es"

Catalogue = dict[str, dict[str, str]]


class Texts:
    """Los textos de un catálogo ya resueltos a un idioma."""

    def __init__(self, catalogue: Catalogue, language: Language = DEFAULT_LANGUAGE) -> None:
        self.language: Language = language
        base = catalogue.get(DEFAULT_LANGUAGE, {})
        self._entries: dict[str, str] = {**base, **catalogue.get(language, {})}

    def __call__(self, key: str, /, **kwargs: object) -> str:
        """El texto de ``key``, con sus huecos rellenos.

        ``key`` es posicional a la fuerza: los huecos de un texto se pasan por
        nombre y uno de ellos se llama precisamente ``key``, que sin la barra
        chocaría con este parámetro.

        Que una clave inexistente reviente es deliberado: es un error de
        programación, no una entrada del usuario, y sale en los tests.
        """
        template = self._entries[key]
        return template.format(**kwargs) if kwargs else template

    def has(self, key: str) -> bool:
        return key in self._entries


def missing_keys(catalogue: Catalogue) -> dict[str, set[str]]:
    """Claves que le faltan a cada idioma respecto del de referencia.

    Para los tests: un catálogo incompleto se detecta ahí y no en una partida.
    """
    reference = set(catalogue.get(DEFAULT_LANGUAGE, {}))
    return {
        language: reference - set(entries)
        for language, entries in catalogue.items()
        if language != DEFAULT_LANGUAGE
    }
