# Registro manual de observações

O arquivo `registro_observacoes.csv` guarda o que os observadores viram de fato no céu. O `verificar.py` compara essas anotações com as previsões. Com algumas dezenas de noites registradas, dá para calibrar os limiares do `astro_config.toml`.

Uma linha por observador por noite. Se várias pessoas observarem na mesma noite, cada uma preenche a sua linha e o script usa a média. Preencha também as noites ruins ou em que o grupo desistiu de observar: elas são tão importantes quanto as boas.

Pode editar o arquivo em qualquer planilha (LibreOffice, Excel, Google Sheets), desde que salve como **CSV separado por vírgulas, em UTF-8**, sem mudar a primeira linha.

## Colunas

| Coluna | Obrigatória | Formato | O que anotar |
|---|---|---|---|
| `data_noite` | sim | AAAA-MM-DD | Data em que a noite **começou**. Uma observação das 20h do dia 24 às 2h do dia 25 é `2026-11-24`. |
| `hora_inicio`, `hora_fim` | não | HH:MM (horário local) | Período em que houve observação ou avaliação do céu. |
| `observador` | não | texto | Nome ou apelido. |
| `local` | não | texto | Ex.: `Wykrota`. |
| `transparencia` | sim | 1 a 5 | Escala abaixo. |
| `seeing` | sim* | 1 a 5 | Escala abaixo. *Deixe vazio se não deu para avaliar. |
| `nuvens_pct` | não | 0 a 100 | Fração do céu coberta por nuvens, em média, no período. |
| `cirrus` | não | `sim` / `nao` | Havia véu de cirrus (halo na Lua, estrelas "molhadas")? |
| `orvalho` | não | `nenhum` / `leve` / `forte` | Condensação no equipamento. |
| `vento` | não | `calmo` / `moderado` / `forte` | Vento no local. |
| `sqm` | não | mag/arcsec² | Leitura do SQM no zênite, se houver. |
| `fwhm_arcsec` | não | segundos de arco | FWHM médio das estrelas nas fotos ou na guiagem (PHD2), se houver. |
| `notas` | não | texto | Qualquer observação: horário em que fechou, direção de onde vieram as nuvens etc. Se o texto tiver vírgula, deixe a planilha cuidar das aspas. |

## Escala de transparência (1 a 5)

| Nota | Como o céu estava |
|---|---|
| 5 | Céu limpo de ponta a ponta. Via Láctea com detalhes e estrelas fracas até perto do horizonte. |
| 4 | Céu limpo com leve névoa perto do horizonte. Via Láctea bem visível. |
| 3 | Névoa ou cirrus finos, ou nuvens esparsas passando. Via Láctea apagada. Dá para observar com interrupções. |
| 2 | Muitas nuvens ou véu de cirrus contínuo. Só objetos brilhantes. Fotografia de longa exposição inviável. |
| 1 | Encoberto, neblina ou chuva. Sem observação. |

## Escala de seeing (1 a 5)

Baseada na escala de Pickering (1 a 10), observando uma estrela brilhante em alta ampliação:

| Nota | Pickering | Como a imagem estava |
|---|---|---|
| 5 | 9–10 | Disco de difração estável, anéis completos e parados. Detalhes finos em planetas. |
| 4 | 7–8 | Anéis visíveis com tremor ocasional. Bons momentos frequentes. |
| 3 | 5–6 | Anéis quebrados, imagem ondulando. Bons momentos esporádicos. |
| 2 | 3–4 | Disco borrado e saltando. Alta ampliação inútil. |
| 1 | 1–2 | Estrela como uma mancha fervendo. Mesmo baixa ampliação sofre. |

Se houver FWHM medido, anote também: ele é a medida mais objetiva de seeing que o grupo pode ter.

## Exemplo

```csv
data_noite,hora_inicio,hora_fim,observador,local,transparencia,seeing,nuvens_pct,cirrus,orvalho,vento,sqm,fwhm_arcsec,notas
2026-11-24,20:30,23:45,João,Wykrota,2,3,60,sim,forte,calmo,19.8,3.1,cirrus a oeste a partir das 22h
2026-11-26,21:00,01:30,Maria,Wykrota,4,4,10,nao,leve,moderado,20.6,,céu abriu depois das 21h
```
