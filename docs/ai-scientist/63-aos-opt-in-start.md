# Bağımsız Lab ve açık AOS opt-in başlatma

Lab bağımsız kullanılabilir; kurulum/test için SWAPP veya AOS gerekmez.
Bu bilgisayardaki hazırlanmış kurulumun kullanıcı başlatma yolu:

```bash
cd /home/cachyos/ai-scientist
bash ops/start-lab.sh
```

Lab arayüzü `http://127.0.0.1:8788` adresindedir. Bu mevcut kurulum yolu,
temiz makine kurucusu değildir; özel principal/suite dosyaları, rol bazlı
DB kimlikleri, `.venv`, derlenmiş arayüz ve mevcut PostgreSQL önceden hazır
olmalıdır. Başlatıcı Lab servislerini hazırlar. Özel fork'un SWAPP alan
eşlemeleri açık çekirdeğin başlatma şartı değildir. Uzaktan erişim için
[README bağlantı komutları](../../README.md#aserdargun-bilgisayarından-bağlanma)
kullanılır.

## AOS üzerinden ayrı araştırma işi

AOS'un normal bootstrap'ı `scripts/serve_desktop.py` içindedir. İzole AOS
kopyasında `src/aos/lab_external.py` ve `MANIFEST.sha256` için
[R4 ek yaması](review-evidence/aos042-wire-stop-package-r4.patch) hazırlanmıştır.
Bu yama dondurulmuş birleşik AOS tabanına uygulanır; farklı canlı AOS
revision'ına körlemesine uygulanmaz. Paralel AOS çalışmasıyla önce dosya
değişikliği koordine edilir. Canlı AOS'a bu teslimatta kurulum yapılmadı.

Yerel dağıtıma ait aşağıdaki değerler önceden seçilir:

| Değer | Gereken bağ |
|---|---|
| Lab API URL | Loopback HTTP; API servisi ve native suite registry hazır |
| Lab token dosyası | Kullanıcının sahip olduğu normal dosya, mode `0600`; API principal `origin=aos` |
| Suite ID | Native registry ve AOS allowlist içinde aynı exact ID |
| AOS workspace/SQLite | Ayrı test oturumu için ayrı özel yollar |
| Console portu | Seçilmiş, boş loopback portu; mevcut AOS oturumuyla çakışmaz |
| Yerel Decider ve broker | Pinli yerel model ortamı ve hazır ortak GPU broker; adil sınırlı çağrı profili |

Normal AOS komutunun örnek şekli aşağıdadır. `/absolute/private/...` yolları,
suite ID ve portlar yerel dağıtımın gerçek değerleriyle değiştirilir. Tokenın
kendisi komut satırına yazılmaz:

```bash
cd /absolute/path/to/coordinated-aos-copy
.venv/bin/python scripts/serve_desktop.py \
  --port 18765 \
  --workspace /absolute/private/aos-lab/workspace \
  --database /absolute/private/aos-lab/trajectory.sqlite \
  --trajectory-database /absolute/private/aos-lab/trajectory.sqlite \
  --engine decider --reuse-decider --shared-gpu-turns \
  --lab-external-api-url http://127.0.0.1:8766 \
  --lab-external-token-file /absolute/private/aos-lab/lab-api.token \
  --lab-external-suite registered.suite.id
```

Üç `--lab-external-*` seçeneği birlikte verilir; birden çok suite için
`--lab-external-suite` tekrarlanabilir. Normal bootstrap, gerçek Decider ve
`--shared-gpu-turns` şartını denetler; fixture karar motoru bu komutla normal
Lab entegrasyonu yerine sunulmaz. Broker/model manifest yolları ve host kaynak
profili dağıtımın AOS yapılandırmasına göre ayrıca hazır olmalıdır.

Authenticated AOS console'da yeni HUMAN lease alınır; exact allowlist suite
ve sınırlı bütçeyle Lab işi başlatılır. İş kalıcı Lab kuyruğuna gider;
normal Lab dispatcher ayrı yürütür ve AOS foreground scheduler slotunu
tutmaz. AOS SQLite, Lab PostgreSQL kimliklerine referans saklar; iki ürün
kendi DB'sine yazar.

Restart sonrası eski lease yeniden başlangıç izni değildir. Yeni açık HUMAN
lease ve recovery ile mevcut handle yeniden bağlanır; yeni Lab start
gönderilmez. Yeni binding altındaki stop intent'i önceki `uncertain` intent'i
korur; aynı binding ile tekrarlanan stop aynı action anahtarını kullanır.
Stop yanıtı tek başına worker drain veya terminal rapor kanıtı değildir.

## Ölçülmüş teslim sınırı

[R4 paket kaydı](review-evidence/aos042-package-r4-summary.json): ayrı kaynak
kopyasında required package validation **4360 kontrol, gerçek exit 0**;
check/apply ve 1095 kaynak üyede byte eşliği. Mevcut dar Lab testleri R3
kaynağında 26/26 geçti; dış test shell exit metadatasının korunmadığı sınır
[CPU icra özetinde](review-evidence/aos042-cpu-lifecycle-summary.json) açıktır.

Gerçek CPU icrası ayrı AOS child, SQLite reopen, Lab HTTP, typed lease/policy,
TCP console outage ve aynı run üzerinde explicit recovery/idempotent stop
yollarını ölçtü. Desktop ve karar motoru fixture'dır. Bu belgede normal
Decider/shared-GPU bootstrap komutu çalıştırılmadı. Canlı AOS kurulumu,
gerçek modelle Lab başlatma ve tam AOS/Lab GPU birlikte çalışma kabulü
ayrıca ölçülmelidir; M0.AOS.2/.3 içindeki ölçülmemiş kabul maddeleri korunur.
