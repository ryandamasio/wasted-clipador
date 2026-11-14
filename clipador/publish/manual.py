"""Publisher manual: o agendador cria a agenda, a fila web mostra o que postar e quando; o dono posta e marca."""
from __future__ import annotations

from clipador.publish.base import Publisher


class ManualPublisher(Publisher):
    name = "manual"
    manual = True

    def configured(self) -> tuple[bool, str]:
        return True, "manual (poste pelo app e marque na fila)"
