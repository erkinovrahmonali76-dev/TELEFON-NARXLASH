
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

const joinBtn = document.getElementById("joinBtn");
const skipBtn = document.getElementById("skipBtn");
const statusText = document.getElementById("status");
const videoGrid = document.getElementById("videoGrid");

let room = null;
let joined = false;

function showStatus(text) {
  statusText.textContent = text;
}

async function leaveRoom() {
  if (room) {
    await room.disconnect();
    room = null;
  }

  joined = false;
  videoGrid.innerHTML = `
    <div class="video-card waiting">
      <div class="avatar">👥</div>
      <p id="status">Guruh xonasi kutilyapti...</p>
    </div>
  `;
}

async function joinRoom() {
  if (!tg.initData) {
    showStatus("Telegram orqali oching.");
    return;
  }

  joinBtn.disabled = true;
  showStatus("Guruh qidirilmoqda...");

  try {
    const response = await fetch("/api/room-token", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        initData: tg.initData
      })
    });

    if (!response.ok) {
      throw new Error("Xona topilmadi");
    }

    const data = await response.json();

    if (!data.url || !data.token) {
      throw new Error("Video server ma'lumotlari yetishmayapti");
    }

    room = new LivekitClient.Room();

    room.on(
      LivekitClient.RoomEvent.TrackSubscribed,
      (track, publication, participant) => {
        const element = track.attach();
        element.autoplay = true;

        const card = document.createElement("div");
        card.className = "video-card";
        card.appendChild(element);
        videoGrid.appendChild(card);
      }
    );

    room.on(LivekitClient.RoomEvent.Disconnected, () => {
      joined = false;
      showStatus("Ulanish uzildi.");
      joinBtn.disabled = false;
    });

    await room.connect(data.url, data.token);

    await room.localParticipant.setCameraEnabled(true);
    await room.localParticipant.setMicrophoneEnabled(true);

    joined = true;
    showStatus("Guruhga ulandingiz!");
    joinBtn.textContent = "✅ Ulandingiz";

  } catch (error) {
    showStatus("Ulanishda xatolik: " + error.message);
    joinBtn.disabled = false;
  }
}

joinBtn.addEventListener("click", joinRoom);

skipBtn.addEventListener("click", async () => {
  await leaveRoom();
  joinBtn.disabled = false;
  joinBtn.textContent = "🌐 Boshqa guruhga o'tish";
  showStatus("Yangi guruh qidirishga tayyor.");
});

document.getElementById("micBtn").addEventListener("click", async () => {
  if (!room || !joined) return;

  const enabled = room.localParticipant.isMicrophoneEnabled;
  await room.localParticipant.setMicrophoneEnabled(!enabled);
});

document.getElementById("cameraBtn").addEventListener("click", async () => {
  if (!room || !joined) return;

  const enabled = room.localParticipant.isCameraEnabled;
  await room.localParticipant.setCameraEnabled(!enabled);
});

document.getElementById("reportBtn").addEventListener("click", async () => {
  if (!room || !joined) {
    showStatus("Avval guruhga ulaning.");
    return;
  }

  showStatus("Shikoyat moderatorga yuborilishi kerak.");
});
