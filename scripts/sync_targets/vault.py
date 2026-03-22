class VaultSyncClient:
    def __init__(self, github_client, output_filename: str):
        self.github_client = github_client
        self.output_filename = output_filename

    def sync(self, config: dict, generated_mds: dict[str, dict]) -> None:
        for lang, data in generated_mds.items():
            vault_path = self._build_vault_path(config, lang)
            is_pushed = self.github_client.push_file(
                config["repo"],
                vault_path,
                data["content"],
                config.get("commit_message", "automated update"),
                config["pat"],
            )
            if not is_pushed:
                raise RuntimeError(f"Vault 同步失败: {config['repo']}/{vault_path}")

    def _build_vault_path(self, config: dict, lang: str) -> str:
        vault_dir = config.get("path", "GitHub-Stars/")
        if not vault_dir.endswith("/"):
            vault_dir += "/"
        return f"{vault_dir}{self.output_filename}_{lang}.md"
