Os bancos de dados do DATASUS que são trabalhados neste programa: 
- **SINAN**: notificações/casos sobre determinados agravos (no caso, meningite)
- **SIM**: óbitos registrados
- **CIHA**: internações/atendimentos hospitalares e/ou ambulatorais.
- **SIH/RD**: registros de AIH aprovada, com análise dos campos CID, estabelecimentos/mantenedoras e perfil demográfico.

## Integração SIH/RD (05/10/2026)

**Novo gráfico — total unificado principal ou secundário:** em **SIH → Diagnósticos e CID**, a série anual/mensal reúne as linhas com qualquer CID do recorte meningite/encefalite em `DIAG_PRINC`, `DIAG_SECUN` ou `DIAGSEC1`–`DIAGSEC9` (incluindo aliases reconhecidos). Cada linha entra **uma única vez**, mesmo com vários CIDs distintos ou repetidos nesses campos. Não há soma das frequências por CID nem inclusão de `CID_MORTE`, `CID_ASSO` ou `CID_NOTIF`. Os filtros gerais e a referência temporal escolhida se aplicam ao novo gráfico.

O CSV permite conferir: **apenas principal + apenas secundário + ambos = total**. Registros elegíveis sem data reconhecida permanecem no total/CSV quando não há filtro de ano, mas não no eixo temporal; lacunas não são preenchidas. Sem campos de data, mostra-se uma barra com o total. A unidade continua sendo a linha de AIH/RD, não pessoa ou internação única; a união não deduplica arquivos sobrepostos nem AIHs por `N_AIH`. Para incluir diagnósticos exclusivamente secundários, carregue um banco com esse recorte ou qualquer CID: o banco extraído somente por principal não recupera esses registros.

Acesse **SIH** no menu lateral, escolha **Upload DuckDB** e envie um banco produzido pelos scripts R de meningite. A tabela de dados é priorizada e `metadados_execucao` não é oferecida como tabela clínica. Também são aceitos Parquet, CSV e DBF. DBC deve ser previamente convertido pelo pipeline R.

São analisados `DIAG_PRINC`, `DIAG_SECUN`, `DIAGSEC1` a `DIAGSEC9`, `CID_MORTE`, `CID_ASSO`, `CID_NOTIF` e outros campos de códigos CID detectados. Há distribuição por campo, presença dos CIDs de meningite/encefalite, seleção conjunta dos secundários e dos campos CID, série temporal opcional por ano/mês e distribuição por `CGC_HOSP`/`CGC_HOSPITAL`. Os códigos originais são preservados; CIDs fora do recorte também aparecem quando a opção de restringir a meningite/encefalite está desligada.

Os percentuais por CID usam todas as linhas filtradas. Cada CID distinto conta uma vez por linha dentro da seleção, mesmo que apareça repetido em vários campos. Uma linha com vários CIDs pode participar de várias categorias. Isso não se aplica ao novo total unificado, em que cada linha conta apenas uma vez.

**Estabelecimentos e mantenedoras:** seleção independente de `CGC_HOSP`/`CGC_HOSPITAL`, `CNES`, `CGC_MANT` e `CNPJ_MANT`, com gráficos, tabela completa e CSV. Identificadores mantêm zeros à esquerda, sem junções nem nomes inferidos. Top + "Outras categorias" preserva o denominador integral. Os dois campos de mantenedora não são coalescidos nem somados.

**Análise demográfica:** faixa etária, pirâmide por sexo, sexo, raça/cor, escolaridade (`INSTRU`), etnia, ano de nascimento, município de residência/estabelecimento e nacionalidade, conforme os campos presentes. Todos ficam na seção demográfica, não na análise de CID. O filtro geral inclui agora a união principal/secundário, permitindo estudar o perfil dessa mesma população.

Idade registrada usa `IDADE`/`COD_IDADE`: 2=dias, 3=meses, 4=anos, 5=100+idade; código 0, unidade desconhecida e idade inválida não são convertidos em anos. Conversão de dias usa 365,25 dias/ano. A distribuição inclui idade ausente/inválida, enquanto a pirâmide usa apenas idade válida e sexo reconhecido (1=masculino, 3=feminino), explicitando os excluídos. Quando `NASC` e `DT_INTER` existem, uma opção separada permite calcular idade aproximada por diferença de datas/365,25, sem substituir a idade registrada. Não há exportação de datas de nascimento individuais nessa análise.

Raça/cor usa o domínio específico do SIH: 01=branca, 02=preta, 03=parda, 04=amarela, 05=indígena, 99=sem informação, conforme a [Portaria SAS 719/2007](https://bvsms.saude.gov.br/bvs/saudelegis/sas/2007/prt0719_28_12_2007.html). Idade e instrução seguem as convenções históricas verificadas na [implementação do microdatasus](https://github.com/rfsaldanha/microdatasus/blob/master/R/process_sih.R), sem reutilizar os dicionários de SIM/SINAN. Escolaridade 0/9 é sem informação, não ausência de escolarização. Etnia/nacionalidade/municípios preservam códigos sem inferir descrições; etnia ausente de raça conhecida não indígena é não aplicável. Distribuições simples mantêm ausência/códigos não interpretados e percentuais sobre todas as linhas filtradas.

A unidade contada é a **linha de AIH/RD**, não pessoa nem internação única. Os filtros R se sobrepõem: não mescle recortes de diagnóstico principal, causa da morte, associado e qualquer CID como conjuntos independentes. `CRITERIO_CID` mostra a seleção aplicada antes do upload. Um filtro na interface restringe esse conjunto, mas não recupera linhas excluídas na origem. `CID_MORTE` não substitui a causa básica qualificada pelo SIM. Zeros, campos vazios e conteúdos sem formato CID-10 são discriminados; formato reconhecido não equivale a validação no dicionário.

**Competência de processamento** é a referência temporal padrão quando ano e mês estão disponíveis. Pode-se optar por data de internação ou saída; estas podem pertencer a outro período. Os totais mantêm registros sem data quando nenhum filtro de ano é aplicado. A série temporal usa somente datas reconhecidas e não transforma lacunas em zero.

Os Parquets anuais do SIH/RD **MENINGITE_qualquer_campo** também estão na release pública, de 1998 a 2026 (2026 parcial conforme os arquivos disponíveis). Selecione manualmente os anos desejados em **SIH → Bancos hospedados no github (Parquets)**; a seleção inicial é vazia e o limite padrão é 68 arquivos, suficiente para carregar todos os 29 anos. Em **Desempenho e memória**, o padrão é 8 GB para o DuckDB e 6 threads, ambos ajustáveis conforme a máquina. A área de comparação aceita SINAN, SIM, CIHA e SIH diretamente ou bases reaproveitadas das abas.

## Comparação de óbitos e gap entre bases

Na seção **Comparação entre bancos de dados**, carregue ou selecione SINAN, SIM, CIHA e SIH. O gap **SINAN × SIM** compara óbitos por meningite no SINAN (todos os casos, confirmados ou ambos) com causa básica ou menção de CID no SIM. O gap **SINAN × CIHA/SIH** compara casos do SINAN (todos ou confirmados) com todos os atendimentos CIHA e internações SIH que tenham CID de meningite/encefalite, sem filtrar por óbito. No SIH, escolha diagnóstico principal, principal + secundário, CID associado ou CID de notificação. No modo anual, os anos de borda são incluídos e limitados às datas exatas de cobertura comum; eles podem representar apenas parte do ano. Os totais CIHA + SIH são uma soma aritmética, sem deduplicação ou linkage entre sistemas.

Os gráficos apresentam as contagens e a diferença assinada (comparador menos SINAN) por ano ou mês completo no intervalo temporal comum, além de barras com os totais de cada série. Os somatórios aparecem também em **Como ler**. Para a visão assistencial, a barra **CIHA + SIH** é uma soma aritmética dos registros dos dois sistemas, sem linkage nem deduplicação; pode haver sobreposição e ela não representa pessoas únicas. Datas e unidades diferem entre sistemas, portanto o gap não estima subnotificação nem letalidade. Linhas com data ausente/inválida são informadas e excluídas do eixo temporal. A inclusão no SIH depende do campo CID e do recorte selecionado; não depende do campo `MORTE`.

### Outros campos propostos (sem implementação de gráficos)

1. `N_AIH, IDENT, SEQ_AIH5, SEQUENCIA, REMESSA`: AIH inicial, continuidade e reapresentação; definição de internações únicas.
2. `MORTE, COBRANCA`: desfecho e mortalidade hospitalar.
3. `DIAS_PERM, QT_DIARIAS`: permanência e utilização de leitos.
4. `UTI_TOTAL, UTI_MES_TO, UTI_INT_TO, MARCA_UTI, MARCA_UCI`: terapia intensiva/intermediária.
5. `PROC_REA, PROC_SOLIC, ESPEC, COMPLEX`: procedimentos, especialidade e complexidade.
6. `VAL_TOT, VAL_SH, VAL_SP, VAL_UTI, VAL_UCI, FINANC, FAEC_TP`: valores aprovados e financiamento.
7. `UF_ZI, UF_ARQUIVO` e cruzamentos entre `MUNIC_RES/MUNIC_MOV`: processamento e fluxos territoriais.
8. `CAR_INT, NATUREZA, NAT_JUR, GESTAO, INFEHOSP, TPDISEC1-TPDISEC9`: contexto assistencial e administrativo.

Essas propostas também aparecem na seção **Outros campos propostos** do app. CNES/mantenedoras e campos demográficos deixaram de ser apenas propostas após autorização para implementá-los. Datas/competências são referências/filtros gerais; nascimento é usado somente em agregações demográficas.

Validação: `python -m unittest discover -s tests -v` — 21 testes, incluindo união sem inflação por vários CIDs, exclusão de menções somente em CID_MORTE/ASSO/NOTIF, datas ausentes, filtros, domínios demográficos SIH, centena de anos, idade ignorada, identificação exata dos campos, zeros à esquerda, ausência de datas e navegação das novas áreas. Os testes passaram nas versões local e GitHub. A contagem e as distribuições foram reconciliadas em sete bancos locais, sem modificar seus arquivos. No banco principal/secundário, 21.164 apenas no principal + 3.069 apenas nos secundários + 351 em ambos = 24.584 registros; os 351 em ambos são contados uma vez. A interface foi testada com esse banco real em todas as opções demográficas e de identificação.

Na integração inicial também foram examinadas amostras dos DBCs RJ de janeiro de 1998, 2008, 2016 e julho de 2026. Elas confirmam mudanças no preenchimento de DIAG_SECUN/CID_ASSO/CID_MORTE e DIAGSEC1-DIAGSEC9; um campo zerado em períodos recentes não comprova ausência de doença.

Este aplicativo cumpre duas funções:

1) Baixar os dados do SINAN (meningite; anos 2007 a 2025), SIM (2007 a 2024) e CIHA (2011 a 2025) referentes ao município, ao estado do Rio de Janeiro e a todos os estados, e convertê-los para os respectivos formatos parquet e duckdb, para fins de análise epidemiológica.

2) Fornecer uma plataforma dinâmica de análise de dados via streamlit.

# Baixando os bancos de dados e convertendo-os
Ao extrair o arquivo "Scripts" em formato RAR, haverão scripts separados para as diferentes etapas - baixar os arquivos do datasus, processar e compilar o que foi baixado para o formato parquet e para o formato duckdb, separado por ano. Bastar executar os scripts. Preferiu-se não unificar os arquivos para que o usuário tenha liberdade de escolher o que baixar. 
Alternativamente, pode-se baixar os arquivos já compilados diretamente através dos "Banco de Dados" em formato .RAR.

Quando os bancos de dados em .dbc são convertidos para .parquet, alguns filtros são aplicados para restringir quais casos são relevantes para a análise epidemiológica da meningite, da encefalite e da meningoencefalite. Além disso, como os dados disponibilizados pelo CIHA são separados por mês para cada respectivo ano, optou-se por mesclar os meses referentes a um dado ano, com a finalidade de analisar mais facilmente os casos referentes a um dado ano.

Em um primeiro momento, os CID-10 utilizados eram os principais códigos diretamente associados a meningite: A17.0 , A39.0 , A87 , G00 , G01 , G02 , G03 , G04 e G05. Contudo, no SIM e na CIHA havia um problema importante: muitos CID-10 que descrevemmeningite, encefalite ou meningoencefalite aparecem como códigos próprios, e nãonecessariamente dentro dos grupos prefixados G00 a G05.
Por exemplo, B58.2 (meningoencefalite por Toxoplasma) e B01.1 (encefalite por varicela) deveriam ser considerados no recorte de análise neurológica infecciosa, mas podem aparecer como CID-10 avulsos nos bancos brutos. Para contornar esse impasse, foi feita uma busca
explícita por CID-10 que incluem meningite, encefalite e/ou meningoencefalite sem incluir outras condições de forma ampla demais. Desse modo, atualmente os CID-10 analisados incluem: 
- Prefixados: G00* , G01* , G02* , G03* , G04* , G05* , A83* , A84* , A85* , A86* , A87* , B06* .
- Avulsos/específicos: A17.0 , A22.8 , A32.1 , A39.0 , B00.3 , B00.4 , B01.0 , B01.1 , B02.0, B02.1 , B05.0 , B05.1 , B26.1 , B26.2 , B37.5 , B38.4 , B45.1 , B57.4 , B58.2 , B60.2.

Referências utilizadas:
http://www2.datasus.gov.br/cid10/V2008/WebHelp/g00_g09.htm
http://www2.datasus.gov.br/cid10/V2008/cid10.htm

# Em construção - Formulário Digital para Investigação de meningite 

Utilizando XLXsforms, criei um espelho da ficha de investigação de meningite elaborada pelo SINAN. O propósito foi me familiarizar com este formato de planilha e quais possibilidades ela proporciona.
No momento, o formuláro está plenamente funcional, apenas faltando alguns ajustes para aprimorar sua apresentação estética. Caso queira acesso aos dados de preenchimento, favor entrar em contato.

Link: https://ee.kobotoolbox.org/x/ifAQUhNw.
  
# Em construção - Painel Streamlit para análise do banco de dados = SINAN, SIM e CIHA

Este app em Python foi feito para análise epidemiológica a partir de arquivos `.parquet ou .duckdb` do DATASUS, com foco nos três bancos de dados supracitados.

Link para a versão mais atual do streamlit (acessível de qualquer dispositivo): https://fgwybuegynhnli87zeyurr.streamlit.app/

## _O que o app faz_

- Lista os Parquets da [release atual do observatório](https://github.com/borbito123/Observatorio-para-meningite-e-encefalite/releases/tag/Release1); o usuário escolhe manualmente quais bases e anos carregar. A release inclui SINAN, SIM, CIHA e SIH/RD para o Rio de Janeiro, incluindo os 29 Parquets anuais `SIH_RD_MENINGITE_qualquer_campo_1998.parquet` a `SIH_RD_MENINGITE_qualquer_campo_2026.parquet`.
- Também aceita **upload** dos parquets / duckdbs que o usuário escolher.
- Fornece um breve dicionário operacional para guiar o usuário em relação aos campos mais relevantes para análise epidemiológica;
- Gera gráficos epidemiológicos interativos.
- Permite download em CSV das tabelas agregadas de cada gráfico.

_Observação: Para contornar eventuais problemas de memória ou crashes do aplicativo, foram impostas algumas limitações que podem ser modificadas pelo usuário. No canto esquerdo da aba "Orientação" há a opção "desempenho e memória" que permite ajustar essas limitações._

## Metodologia_

Consulte esta seção para se familarizar com os CID-10 envolvidos em meningoencefalites e a tabela-resumo referente aos padrões liquóricos vigentes na literatura atual.

## _Gráficos incluídos_

### Para SINAN
- Indicadores -> Fornece: tabela anual de indicadores; evolução dos casos confirmados; notificações, confirmados, descartados e óbitos; proporções, inconclusivos, ignorados e letalidade; hospitalização em suspeitos/ notificados, confirmados e descartados; prevalência de sinais/sintomas entre os registros; número de comunicantes por quimioprofilaxia; vacinação por classificação final; punção laboratorial; exame quimiocitológico do líquor (LCR); distribuição de glicose, proteínas, neutrófilos e leucócitos; resumo dos parâmetros do LCR.
- Análise temporal -> Fornece: série temporal por ano, mês ou semana; heatmap de sazonalidade ano × mês; estratificação por sexo, CID-10 convertido, grupo etiológico SINAN ou CLASSI_FIN , quando os campos estiverem disponíveis. 
- Análise do CID-10 -> Fornece: distribuição dos casos por classificação final (confirmado, descartado...), distribuição dos casos por conclusão diagnóstica (especifica o grupo etiológico), conversão dos grupos etiológicos preenchidos no banco de dados para os devidos CID-10 (o streamlt mostra a regra usada para converter CON_DIAGES em CID-10), distribuição dos casos conforme evolução, distribuição dos casos por critério diagnóstico utilizado, distribuição conforme realiização de punção laboratorial, gráficos de distribuição dos principais parâmetros liquóricos analisados (glicose, leucócitos, proteínas, neutrófiilos).
- Demografia -> Fornece: distribuição por faixa etária de 5 anos; pirâmide etária por sexo; escolaridade de confirmados e óbitos; distribuição por sexo, raça/cor, município de residência e município de ocorrência/notificação. Nos gráficos municipais, é usado Top N + “Outros municípios”.
- Campos importantes não preenchidos -> Fornece: quantos registros não foram preenchidos conforme certas variáveis de maior relevância
- Prévia -> Fornece: prévia do dados presentes no banco de dados, sendo possível exportar para o formato .CSV

_Explicando o que foi feito na tabela de conversão encontrada no SINAN:_ Originalmente, o SINAN agrupa todos os seus casos sob o CID "G03.9". Caso haja diagnóstico e confirmação, então se especifica a meningite em algumas categorias (veja a seção "Classificação do Caso" em https://portalsinan.saude.gov.br/images/documentos/Agravos/Meningite/Meningite_v5.pdf). Na seção "Análise epidemiológico e CID10", haverá um gráfico de conversão que aloca todos os casos confirmados e os enquadra em algum dos seguintes CID: G00, G01, G02, G03, G04, G05, A39, A17, A87.

A se ponderar: A17 e A39 se enquadrariam no CID G01, mas atualmnte se encontram separadas. Em contrapartida, meningite por haemophilus e meningocóccica já foram incluídas no CID G00. Isso representaria uma certa inconsistência que precisaria ser corrigida.

A referência utilizada para alocação foi: https://portalsinan.saude.gov.br/images/documentos/Agravos/Meningite/Meningite_v5.pdf.

### Para SIM
- Indicadores -> Fornece: óbitos com menção de meningite/encefalite; óbitos com meningite/encefalite como causa básica; comparação entre menção e causa básica; óbito na gravidez ( OBITOGRAV ) por menção e por causa básica; óbito no puerpério ( OBITOPUERP ) por menção e por causa básica, quando o campo existir.
- Análise temporal -> Fornece: série temporal por ano, mês ou semana; heatmap de sazonalidadeano × mês; estratificação por sexo ou tipo CID-10 quando disponível.
- Análise do CID-10 -> Fornece: distribuição dos óbitos conforme o CID-10, gráfico que converte os CID-10 para o padrão utilizado no gráfico de conversão do SINAN. 
- Demografia -> Fornece: distribuição por faixa etária de 5 anos; pirâmide etária por sexo; escolaridade; distribuição por sexo, raça/cor, município de residência e município de ocorrência.
- Campos importantes não preenchidos -> Fornece: quantos registros não foram preenchidos conforme certas variáveis de maior relevância
- Prévia -> Fornece: prévia do dados presentes no banco de dados, sendo possível exportar para o formato .CSV

_Explicando o que foi feito na tabela de conversão encontrada no SIM:_ Por conta do jeito que o banco de dados é preenchido e disponiblizado, muitos CIDs que são incluídos em um dos CIDs prefixados (G00, G01, G02, G03, G04, G05) ficariam perdidos se o script de conversão não procurasse por eles explicitamente. Desse modo, os novos CIDs mencionados na seção "Baixando os bancos de dados e convertendo-os" deste readme.md foram inclusos para evitar que não fossem perdidos. Na seção "Análise etiológica e CID-10", haverá um gráfico de conversão que aloca todos os casos confirmados e os enquadra em algum dos seguintes CID: G00, G01, G02, G03, G04, G05, A39, A17, A87.

A se ponderar: A17 e A39 se enquadrariam no CID G01, mas atualmnte se encontram separadas. Em contrapartida, meningite por haemophilus e meningocóccica já foram incluídas no CID G00. Isso representaria uma certa inconsistência que precisaria ser corrigida. 

### Para CIHA
- Indicadores -> Fornece: Fornece: total de atendimentos; atendimentos com diagnóstico principal de meningite/encefalite; mortes administrativas; modalidade hospitalar ou ambulatorial; procedimentos e quantidade; distribuição dos dias de permanência.
- Análise temporal -> Fornece: série temporal por ano, mês ou semana; heatmap de sazonalidade ano × mês; estratificação por sexo ou tipo CID-10 quando disponível. 
- Análise do CID-10 -> Fornece: distribuição dos atendimentos por tipo CID-10; conversão para adequação ao CID-10 de meningite/encefalite; verificação específica de G01 e G02 ; CID-10 dos registros com morte administrativa.
- Demografia -> Fornece: distribuição por faixa etária de 5 anos; pirâmide etária por sexo; distribuição por sexo, raça/cor, município de residência e município de ocorrência/atendimento.
- Campos importantes não preenchidos -> Fornece: quantos registros não foram preenchidos conforme certas variáveis de maior relevância
- Prévia -> Fornece: prévia do dados presentes no banco de dados, sendo possível exportar para o formato .CSV

_Explicando o que foi feito na tabela de conversão encontrada no CIHA:_ Por conta do jeito que o banco de dados é preenchido e disponiblizado, muitos CIDs que são incluídos em um dos CIDs prefixados (G00, G01, G02, G03, G04, G05) ficariam perdidos se o script de conversão não procurasse por eles explicitamente. Desse modo, os novos CIDs mencionados na seção "Baixando os bancos de dados e convertendo-os" deste readme.md foram inclusos para evitar que não fossem perdidos. Na seção "Análise etiológica e CID-10", haverá um gráfico de conversão que aloca todos os casos confirmados e os enquadra em algum dos seguintes CID: G00, G01, G02, G03, G04, G05, A39, A17, A87.

A se ponderar: A17 e A39 se enquadrariam no CID G01, mas atualmnte se encontram separadas. Em contrapartida, meningite por haemophilus e meningocóccica já foram incluídas no CID G00. Isso representaria uma certa inconsistência que precisaria ser corrigida. 

### Comparação entre bancos de dados
- Comparação temporal (semanas, meses, anos)
- Possibilidade de estratiificar por CID-10, utilizando os gráficos convertidos para facilitar a equivalência.
- Os gráficos de GAP exibem contagens absolutas junto aos pontos das linhas e diferenças em registros junto às barras, mantendo o sinal negativo e os somatórios em “Como ler”.
- No GAP assistencial, o SINAN exige `ATE_HOSPIT=1` e permite escolher confirmados (`CLASSI_FIN=1`), total (qualquer classificação) ou descartados (`CLASSI_FIN=2`) com internação. Na CIHA, o seletor permite hospitalar (`MODALIDADE=01`, padrão), total (inclusive modalidade desconhecida) ou ambulatorial (`MODALIDADE=02`); os filtros-base continuam aplicados. Sem os campos exigidos, o recorte é bloqueado com aviso, sem inferência pela data.
- A série CIHA + SIH aparece nas linhas, totais, barras de GAP e CSVs. Seu GAP é `(CIHA + SIH) − SINAN`, subtraindo o SINAN uma única vez. A soma é aritmética, sem pareamento/deduplicação entre sistemas; no modo ambulatorial/total da CIHA, a comparação não representa somente internações.
- A conversão de datas é compartilhada entre as análises: anos e meses usam o calendário, enquanto semanas usam a convenção ISO. Datas compactas inválidas não devem ser reinterpretadas como anos espúrios; os testes em `tests/test_temporal_gap.py` conferem soma mensal versus anual e os anos de borda.

Observação: A comparação entre bases é **exploratória** e faz mais sentido quando o agravo, o território e a janela temporal são os mesmos.

# _Instalação_

Crie e ative um ambiente virtual, se desejar, e depois instale as dependências:

```bash
pip install -r requirements.txt
```

## _Execução_

No diretório do projeto, rode:

```bash
streamlit run app_streamlit_app.py
```

## _Como usar_

Disclaimer: Atenção ao limite de parquets / duckdbs que estão sendo lidos ao mesmo tempo em uma única seção. Isso pode ser alterado, mas bastante cuidado em quantos arquivos são carregados simultaneamente.

### Opção 1: leitura automática dos parquets disponibilizados na release mais atual do github
Basta selecionar quais anos deseja-se analisar. 

### Opção 2: upload
Envie um ou mais arquivos `.parquet ou .duckdb` na respectiva aba do banco de dados desejado.
