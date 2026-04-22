import { Map as MapView } from "./components/Map";
import { StatusBar } from "./components/StatusBar";

export default function App() {
  return (
    <div style={{ position: "relative", width: "100vw", height: "100vh" }}>
      <MapView />
      <StatusBar />
    </div>
  );
}
