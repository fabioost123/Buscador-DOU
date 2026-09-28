# Buscador DOU — busca diária automática, 100% no GitHub

Todo dia às **08:00 (horário de Brasília)** o GitHub roda um robô que varre o
Diário Oficial da União procurando as suas palavras-chave e atualiza o painel
(`index.html`). Seu computador **não precisa estar ligado** e você não gerencia
nenhum servidor. Custo: zero.

## Como funciona
- `scripts/buscar_dou.py` — o robô. Baixa a "Leitura do Jornal" do DOU (in.gov.br),
  filtra as matérias que citam suas palavras-chave (sem diferenciar maiúsculas
  nem acentos) e guarda tudo em `dados.json`.
- `.github/workflows/buscar-dou.yml` — o agendamento (GitHub Actions), 11:00 UTC = 08:00 Brasília.
- `index.html` — o painel (mesmo visual do Buscador). Só lê o `dados.json`.
- `config.json` — **suas palavras-chave** e as seções do DOU a varrer.

## Instalação (uma vez só, uns 10 minutos)

1. **Conta no GitHub:** crie uma conta gratuita em github.com (se ainda não tiver).
2. **Repositório:** clique em **New repository**. Nome sugerido: `buscador-dou`.
   Deixe **Public** (o GitHub Pages grátis exige repositório público) e crie.
3. **Enviar os arquivos:** extraia o `.zip`. Na página do repositório, clique em
   **uploading an existing file** e arraste **todo o conteúdo da pasta** —
   inclusive as pastas `.github` e `scripts`. Clique em **Commit changes**.
   - Se a pasta `.github` não subir, use **Add file → Create new file**, digite o
     nome `.github/workflows/buscar-dou.yml` e cole o conteúdo do arquivo.
4. **Ligar o site (GitHub Pages):** **Settings → Pages →** em *Build and deployment*
   escolha **Deploy from a branch**, branch **main**, pasta **/ (root)**, **Save**.
   Em 1–2 minutos aparece o endereço: `https://SEU_USUARIO.github.io/buscador-dou/`
5. **Permissão do robô:** **Settings → Actions → General → Workflow permissions →**
   marque **Read and write permissions → Save** (só é necessário se o passo
   "Salvar resultados" reclamar de permissão).
6. **Primeiro teste (importante):** aba **Actions → Buscar DOU → Run workflow**.
   Se o GitHub pedir para habilitar os workflows, confirme. Em ~1 minuto o ✅
   aparece. Abra o site: a tabela **Execuções** mostra quantas matérias foram varridas.

Pronto. Dali em diante roda sozinho todo dia às 8h.

## Uso no dia a dia
- **Mudar as palavras-chave:** no GitHub, abra `config.json` → ícone de lápis →
  edite → **Commit changes**. A próxima busca já usa a lista nova.
- **Incluir outras seções:** em `config.json`, `"secoes": ["do1", "do3"]`
  (DO1 = atos normativos/portarias, DO2 = pessoal, DO3 = contratos/editais/extratos).
- **Buscar uma data específica:** Actions → Buscar DOU → Run workflow → digite `dd-mm-aaaa`.
- **Se a busca falhar:** o GitHub envia e-mail, a tabela **Execuções** mostra o
  erro e o log completo fica em Actions.

## Avisos honestos
- **A chamada real ao in.gov.br não foi testada por mim** (meu ambiente não tem
  internet). O robô e o painel foram testados com dados simulados na estrutura
  que outros projetos abertos descrevem para a página do DOU. O passo 6 é o
  teste de verdade — se der erro, me mande a mensagem que aparece em Execuções.
- **Horário aproximado:** o GitHub costuma atrasar execuções agendadas em alguns
  minutos (às vezes mais em horários de pico).
- **A busca olha título, órgão e a prévia da matéria** (o texto que o próprio DOU
  publica na listagem). Uma palavra que só aparece no meio do texto completo
  pode não ser detectada.
- **O repositório é público:** ficam visíveis o `config.json` (suas palavras-chave)
  e o `dados.json` (conteúdo do DOU, que já é público).
- **Marcações de "revisado" e itens manuais** ficam só no navegador que você usa
  (não sobem para o GitHub).
- Se o repositório ficar 60 dias sem nenhuma atividade, o GitHub pausa os
  agendamentos. Como o robô grava um registro todo dia, isso não deveria acontecer;
  se acontecer, Actions → Buscar DOU → **Enable workflow**.
- Ao abrir o `index.html` direto do computador (duplo clique), o navegador bloqueia
  a leitura do `dados.json`; o painel avisa e deixa você selecionar o arquivo à mão.
  Pelo endereço do GitHub Pages funciona normalmente.
