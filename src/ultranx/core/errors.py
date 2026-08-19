"""Hierarquia de exceções do UltraNX.

Toda falha esperada é convertida numa destas classes antes de cruzar a fronteira
entre worker thread e UI, para que a interface exiba mensagem acionável em vez
de traceback bruto. ``guidance`` carrega a orientação de recuperação manual.
"""

from __future__ import annotations


class UltraNXError(Exception):
    """Erro base. Toda exceção do domínio herda desta."""

    default_guidance: str = "Reinicie o UltraNX e tente novamente."

    def __init__(self, message: str, guidance: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.guidance = guidance or self.default_guidance

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class DriveError(UltraNXError):
    """Problemas de detecção, seleção ou validade da mídia removível."""

    default_guidance = (
        "Reinsira o cartão SD, confirme que está formatado em FAT32/exFAT e "
        "selecione a raiz manualmente se necessário."
    )


class DriveDisconnectedError(DriveError):
    """A mídia desapareceu no meio da operação."""

    default_guidance = (
        "O cartão foi desconectado durante a operação. O SD pode estar em estado "
        "parcial: reinsira o cartão e execute o UltraNX novamente ANTES de bootar "
        "o console."
    )


class NetworkError(UltraNXError):
    """Falha de rede, DNS, TLS, timeout ou HTTP não-2xx."""

    default_guidance = (
        "Verifique sua conexão com a internet e tente novamente. Se o problema "
        "persistir, o servidor do pacote pode estar temporariamente indisponível."
    )


class RemoteDataError(UltraNXError):
    """Resposta remota presente mas inválida (manifest/versão malformados)."""

    default_guidance = (
        "Os dados do servidor estão inconsistentes. Aguarde alguns minutos e "
        "tente novamente ou reporte o problema aos mantenedores."
    )


class IntegrityError(UltraNXError):
    """Checksum ou tamanho do payload divergente do manifest."""

    default_guidance = (
        "O download foi corrompido e NÃO foi aplicado ao SD. Repita a operação; "
        "se falhar de novo, troque de rede."
    )


class PermissionDeniedError(UltraNXError):
    """Sem permissão de escrita/remoção no caminho alvo."""

    default_guidance = (
        "Feche programas que estejam usando o cartão (explorador de arquivos, "
        "antivírus, players) e execute o UltraNX com permissão de escrita no SD. "
        "No Linux, confirme que a mídia não está montada como somente-leitura."
    )


class SanitizerError(UltraNXError):
    """Falha durante a limpeza seletiva."""

    default_guidance = (
        "A limpeza foi interrompida. Nenhum arquivo protegido pela whitelist foi "
        "afetado. Consulte o log e remova manualmente a pasta indicada."
    )


class InstallError(UltraNXError):
    """Falha ao baixar, extrair ou finalizar a instalação."""

    default_guidance = (
        "A instalação não foi concluída. Consulte o log, mantenha o cartão "
        "conectado e execute o UltraNX novamente para refazer a gravação."
    )


class OperationCancelled(UltraNXError):
    """Cancelamento solicitado pelo usuário (não é erro fatal)."""

    default_guidance = (
        "Operação cancelada. O SD pode estar em estado parcial: rode a "
        "atualização novamente antes de usar o console."
    )
