# 🛰️ Radar de Vagas — Estágio/Júnior em Dados & IA

Busca vagas novas 3x por dia, filtra por **área** (dados, IA, ML, analytics, BI…), **nível** (estágio, júnior, trainee) e **modalidade** (remoto em qualquer lugar; híbrido/presencial só em Lavras/MG), e manda só as **novas** no seu Telegram. Roda de graça no GitHub Actions — não precisa deixar o PC ligado.

```
GitHub Actions (cron 3x/dia) → vagas.py → Gupy / Greenhouse / Lever
                                   │ filtros (config.yaml)
                                   │ dedup (data/seen.json, commitado de volta)
                                   └→ Telegram
```

## 1. Criar o bot no Telegram (2 min)

1. No Telegram, abra o **@BotFather** → `/newbot` → escolha nome e usuário. Ele te dá o **token** (`123456:ABC...`).
2. Abra o seu bot novo e mande um `oi` (sem isso ele não consegue te mandar mensagem).
3. Descubra seu `chat_id`:

```bash
cd radar-vagas
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_TOKEN='COLE_O_TOKEN_AQUI'
python vagas.py --get-chat-id
```

4. Teste o envio:

```bash
export TELEGRAM_CHAT_ID='NUMERO_QUE_APARECEU'
python vagas.py --test-telegram
```

## 2. Testar a busca localmente

```bash
python vagas.py --dry-run        # lista as vagas que passariam; não envia nem salva
python vagas.py --dry-run -v     # idem + motivo de cada descarte (bom pra calibrar filtros)
```

## 3. Subir pro GitHub e agendar

Repositório **privado** (o histórico de vagas fica lá). Com o `gh` (`sudo dnf install gh` no Fedora):

```bash
gh auth login
git init && git add . && git commit -m "Radar de vagas"
gh repo create radar-vagas --private --source=. --push

gh secret set TELEGRAM_TOKEN   --body "$TELEGRAM_TOKEN"
gh secret set TELEGRAM_CHAT_ID --body "$TELEGRAM_CHAT_ID"

gh workflow run radar.yml        # primeira execução agora
gh run watch                     # acompanhar
```

Sem `gh`: crie o repo no site, dê push, e cadastre os dois secrets em **Settings → Secrets and variables → Actions**. Para rodar na hora: aba **Actions → Radar de Vagas → Run workflow**.

A **primeira execução** só marca as vagas abertas como vistas e manda um resumo com 5 delas — dali em diante chegam só as novas.

## 4. Ajustar filtros

Tudo fica no `config.yaml` (sem mexer no código):

| Chave | O que faz |
|---|---|
| `gupy_termos` | buscas feitas na Gupy (adicione/remova à vontade) |
| `area` | o título precisa ter pelo menos um desses termos |
| `nivel` | estágio/júnior/trainee (vagas do tipo Estágio/Trainee na Gupy passam direto) |
| `excluir` | descarta se aparecer no título (sênior, pleno, II, gerente…) |
| `aceitar_programas_genericos` | deixa passar "Programa de Estágio 2027" se a descrição falar de dados/IA |
| `locais_presencial_hibrido` | onde você aceita híbrido/presencial (lista vazia = qualquer lugar) |
| `greenhouse` / `lever` | empresas específicas (identificador da URL da página de vagas) |
| `max_dias_publicacao` | ignora vagas mais antigas que isso |

Depois de editar: `git commit -am "ajusta filtros" && git push`.

Pra mudar o horário, edite o `cron` em `.github/workflows/radar.yml` (está em UTC; Brasília = UTC−3).

## Observações

- A Gupy concentra a maioria dos programas de estágio grandes no Brasil (iFood, Serasa, Itaú, Ambev…). A API usada é a interna do portal de candidatos (`portal.gupy.io/api/job-search/jobs`, adotada após a Gupy desativar os endereços antigos em out/2026). Se ela mudar de novo, o script **falha sem mexer no histórico** (você vê o ❌ na aba Actions e recebe e-mail do GitHub).
- LinkedIn e Indeed ficaram de fora de propósito: os termos de uso proíbem scraping e eles bloqueiam rápido. Para eles, use os alertas de vaga nativos (LinkedIn → Vagas → "Criar alerta").
- Em repositórios sem atividade por 60 dias o GitHub pode pausar o agendamento; se chegar e-mail avisando, é só reativar na aba Actions.
