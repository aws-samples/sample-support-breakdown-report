**Importante:** Este é um projeto de amostra apenas para fins de demonstração. Ele não se destina ao uso em produção sem uma revisão, teste e endurecimento minuciosos.

![logo](/media/logo.png)

# Support Breakdown Report

*Leia em outro idioma: [English](README.md) (versão principal).*

Gera um CSV das cobranças do AWS Enterprise Support, com uma linha por linked account. O relatório pode cobrir um único mês de faturamento ou uma janela de look-back de vários meses.

## Como funciona

A ferramenta obtém todos os dados de três APIs do AWS Billing e as combina em uma linha de CSV por linked account, por mês de faturamento:

| API | Fornece |
|-----|---------|
| `get-enterprise-support-contract-details` | Plano de preços, método de alocação e quais contas são cobradas (e em qual percentual). |
| `get-enterprise-support-charge-summary` | Totais do mês: gasto total elegível ao Support e cobrança total de Support. |
| `list-enterprise-support-linked-account-charges` | Detalhamento por linked account: gasto elegível, gasto rateado, segundos faturáveis e períodos de link/assinatura (paginado). |

## Requisitos

- Python 3.10+
- `boto3 >= 1.43.0` — as APIs de billing do Enterprise Support só existem a partir da linha 1.43.x (não estão na 1.42.x). O script detecta um SDK antigo e avisa para atualizar, em vez de falhar de forma obscura.
- Credenciais AWS com permissão para chamar as APIs de billing do Enterprise Support.

O boto3 já vem instalado no AWS CloudShell, então lá não é preciso configurar nada.

## Instalação


Clone o repositório:
  
```bash
git clone https://github.com/aws-samples/sample-support-breakdown-report.git
cd sample-support-breakdown-report
```

### AWS CloudShell

O boto3 já está disponível. Apenas garanta que esteja atualizado:

```bash
pip install --upgrade boto3
```

### Máquina local

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Configure as credenciais com um profile nomeado, variáveis de ambiente ouqualquer método suportado pela cadeia de credenciais do AWS SDK.

## Uso

Use o console interativo. O `console.py` é um pequeno REPL; o comando `run` gera o relatório, repassando qualquer opção para ele.

```bash
python console.py
```

```
support-breakdown> run
support-breakdown> run --lookback 3
support-breakdown> run --billing-month 2026-07 --output report.csv
support-breakdown> help
support-breakdown> quit
```

### Opções

Passe estas opções para o comando `run`:

| Opção | Padrão | Descrição |
|-------|--------|-----------|
| `--lookback N` | `1` | Número de meses de faturamento a incluir, contando para trás a partir do mês final. |
| `--end-month YYYY-MM` | último mês completo | Mês de faturamento mais recente a incluir. |
| `--billing-month YYYY-MM` | – | Gera um único mês. Sobrepõe `--lookback`/`--end-month`. |
| `--output PATH`, `-o PATH` | `support-breakdown-report-<timestamp>.csv` | Caminho do CSV de saída, ou `-` para stdout. Quando omitido, um sufixo de timestamp é adicionado para não sobrescrever execuções anteriores. |
| `--profile NAME` | ambiente/default | Profile nomeado da AWS a usar. |

## Formato de saída

O CSV usa terminações de linha CRLF. As linhas são agrupadas por payer account e ordenadas de forma crescente por account id dentro de cada grupo.

### Colunas

| Coluna | Origem na API | Definição oficial |
|--------|---------------|-------------------------|
| `description` | contrato `pricingPlans[0].description` | Descrição do plano de preços do cliente naquele mês (ex.: `Partner Led Pricing Plan 2.0`, `Public Pricing Plan`). Após a migração para o CuPPA, pode não ser totalmente consistente. |
| `charge_allocation_method` | contrato `supportAllocationMethod` | Método de cobrança, valor cru da API: `Proportional` ou `Fixed_Percentage` (veja abaixo). |
| `charge_account_id` | linked account `payerAccountId` | Conta à qual a cobrança é aplicada. Para perfis ES / Unified Operations, é a payer account do perfil do cliente. |
| `total_aws_charges` | summary `totalSupportEligibleSpend` | Gasto AWS agregado usado para derivar a cobrança de suporte. Igual à soma de `account_prorated_charges + account_ri_charges + account_sp_charges`. Igual em todas as linhas. |
| `support_charges` | summary `totalSupportCharge` × contrato `chargedPayerAccountIds[].chargePercentage` | Valor cobrado do Charge Account ID daquela linha. `0` significa que essa charge account não foi cobrada no período. |
| `total_support_charges` | summary `totalSupportCharge` | Cobrança total de suporte do perfil de faturamento inteiro, antes da alocação. Igual em todas as linhas. |
| `support_charge_percentage` | derivado: `support_charges ÷ total_support_charges` | Fração da cobrança total de suporte atribuída ao Charge Account ID (`0` quando o total é `0`). |
| `account_id` | linked account `accountId` | A conta (linked ou payer) rastreada sob o perfil de faturamento. |
| `payer_account_id` | linked account `payerAccountId` | A payer account à qual o account id está vinculado. |
| `account_total_charges` | linked account `totalSupportEligibleSpend` (4 casas) | Gasto AWS total usado para calcular a cobrança de suporte, antes do rateio. |
| `account_prorated_charges` | linked account `proratedTotalSupportEligibleSpend` | `account_total_charges × (account_billable_seconds ÷ account_total_seconds)`. |
| `account_total_seconds` | linked account `totalSeconds` | Total de segundos no mês de faturamento. |
| `account_billable_seconds` | linked account `billableSeconds` | Segundos faturáveis com base no período de assinatura da conta. |
| `account_ri_charges` | linked account `totalSupportEligibleReservedInstanceSpend` | Cobranças de compra de Reserved Instance. |
| `account_sp_charges` | linked account `totalSupportEligibleSavingsPlanSpend` | Cobranças de compra de Savings Plan. |
| `account_link_periods` | linked account `linkedTimePeriods` (formatado) | Período de data/hora em que a conta esteve vinculada à payer account. |
| `account_subscription_periods` | linked account `subscriptionTimePeriods` (formatado) | Período de data/hora em que a conta esteve assinante do produto de suporte. |
| `bill_month` | mês do relatório | Período de faturamento como `YYYY-MM` (ex.: `2026-07`). |

### Métodos de alocação de cobrança

O valor em `charge_allocation_method` é o valor cru do campo `supportAllocationMethod` da API, que tem dois valores válidos:

- **Fixed_Percentage** — as cobranças de suporte são distribuídas entre as payer accounts segundo percentuais pré-configurados no contrato (`chargedPayerAccountIds[].chargePercentage`).
- **Proportional** — as cobranças de suporte são distribuídas a cada conta proporcionalmente ao seu eligible spend. Nesse modo, todo `chargedPayerAccountIds[].chargePercentage` é `0.0`, então o valor por conta não vem do contrato e é calculado como `total_support_charge × (account_prorated_charges ÷ Σ account_prorated_charges)`.

> **Nota de implementação:** o modo é tratado como **Proportional** quando qualquer um dos sinais indica isso: o `supportAllocationMethod` do contrato é `Proportional`, ou o contrato tem contas cobradas e **todos** os `chargePercentage` são `0.0`. Nesse caso o relatório distribui a cobrança total de suporte pela participação de cada conta no prorated eligible spend. Caso contrário (`Fixed_Percentage`), usa os percentuais do contrato diretamente, e quando não há contas cobradas os valores por conta ficam `0`.

## Solução de problemas

- **"This boto3/botocore version does not support the Enterprise Support billing APIs"** — atualize o SDK: `pip install --upgrade boto3 botocore`. Garanta que está atualizando o mesmo interpretador/virtualenv com o qual roda o script.
- **"Failed to initialize the AWS billing client"** — normalmente um `--profile` inexistente ou credenciais ausentes. Verifique sua configuração da AWS.
- **`WARN: no linked account charges returned for <mês>`** — as APIs não retornaram dados para aquele mês (por exemplo, um mês sem contrato ativo). Os demais meses do intervalo continuam sendo processados.
- **`AccessDeniedException` — "Caller is not a designated primary payer"** (ao chamar `GetEnterpriseSupportChargeSummary`) — a conta não foi habilitada (onboarded) para usar as APIs de billing do Enterprise Support. Entre em contato com o seu AWS Technical Account Manager (TAM) para solicitar o onboarding.

## Segurança

Consulte [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) para mais informações.

## Licença

Este projeto é licenciado sob a licença MIT-0. Consulte o arquivo [LICENSE](LICENSE).
