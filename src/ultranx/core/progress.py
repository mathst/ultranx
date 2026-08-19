"""Estimativa de tempo restante por etapa.

Duas decisões que sustentam o resto:

* **Média móvel exponencial**, não média simples. Cartão SD tem vazão irregular
  (cache do SO enche, depois escreve em bloco); média simples faz a estimativa
  oscilar de "2 min" para "20 min" e volta, o que é pior que não mostrar nada.
* **Relógio injetável.** O tempo entra por parâmetro (``now``), com
  ``time.monotonic`` como padrão. Isso mantém os testes determinísticos e evita
  que ajuste de horário do sistema estrague a conta no meio de uma gravação.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

# Peso da amostra nova na média móvel. 0.25 reage a mudança real de vazão em
# poucos segundos sem tremer a cada chunk.
_SMOOTHING = 0.25

# Abaixo disto a amostra é ruído (chunk grande chegando de cache).
_MIN_INTERVAL_SECONDS = 0.35

# Não estima antes de ter progresso suficiente: no primeiro 1% qualquer conta
# erra por ordens de magnitude.
_MIN_FRACTION = 0.02


@dataclass(slots=True)
class RateEstimator:
    """Acompanha o ritmo de uma etapa e devolve o tempo restante.

    ``unit`` é livre: bytes no download, entradas na extração, itens na limpeza.
    """

    clock: Callable[[], float] = time.monotonic
    _rate: float | None = field(default=None, init=False)
    _last_time: float | None = field(default=None, init=False)
    _last_done: int = field(default=0, init=False)
    _started_at: float | None = field(default=None, init=False)

    def reset(self) -> None:
        """Zera o estado. Chamado ao entrar numa nova etapa."""
        self._rate = None
        self._last_time = None
        self._last_done = 0
        self._started_at = None

    @property
    def rate(self) -> float | None:
        """Vazão suavizada em unidades por segundo, ou ``None`` sem amostra."""
        return self._rate

    def elapsed(self) -> float:
        return 0.0 if self._started_at is None else self.clock() - self._started_at

    def update(self, done: int, total: int | None) -> float | None:
        """Registra progresso e devolve o tempo restante estimado, em segundos.

        Devolve ``None`` quando ainda não há base para estimar: total
        desconhecido, progresso insuficiente, ou vazão não medida.
        """
        now = self.clock()
        if self._started_at is None:
            self._started_at = now
            self._last_time = now
            self._last_done = done
            return None

        interval = now - self._last_time if self._last_time is not None else 0.0
        if interval >= _MIN_INTERVAL_SECONDS:
            delta = done - self._last_done
            if delta > 0:
                sample = delta / interval
                self._rate = (
                    sample
                    if self._rate is None
                    else self._rate * (1 - _SMOOTHING) + sample * _SMOOTHING
                )
            self._last_time = now
            self._last_done = done

        if not total or total <= 0 or self._rate is None or self._rate <= 0:
            return None
        if done / total < _MIN_FRACTION:
            return None

        remaining = total - done
        return max(remaining / self._rate, 0.0) if remaining > 0 else 0.0


def format_duration(seconds: float | None) -> str:
    """Formata segundos como ``45 s``, ``3 min``, ``1 h 12 min``.

    Arredonda para cima em minutos: prometer menos e entregar mais é melhor que
    o contrário. ``None`` devolve ``calculando…``.
    """
    if seconds is None:
        return "calculando…"
    total = int(max(seconds, 0))
    if total < 10:
        return "poucos segundos"
    if total < 60:
        return f"{total} s"

    minutes = (total + 59) // 60
    if minutes < 60:
        return f"{minutes} min"

    hours, rest = divmod(minutes, 60)
    return f"{hours} h" if rest == 0 else f"{hours} h {rest} min"


def format_rate(units_per_second: float | None, unit: str = "MB/s") -> str:
    """Formata vazão para exibição; ``None`` devolve string vazia."""
    if units_per_second is None or units_per_second <= 0:
        return ""
    if unit == "MB/s":
        return f"{units_per_second / (1024 * 1024):.1f} MB/s"
    return f"{units_per_second:.1f} {unit}"
